"""Core tests: role-based access control and multi-tenant schema isolation.

The isolation tests are the important ones — they prove that data written in one
workspace's Postgres schema is invisible from another, which is the whole safety
promise of the schema-per-tenant design.
"""
import json
from types import SimpleNamespace

from django.db import connection
from django.http import HttpResponse
from django.test import SimpleTestCase, TransactionTestCase
from django_tenants.utils import tenant_context

from modules.crm.models import Company, Contact, Deal

from .events import _coerce_custom_value, _create_contact, _norm_key, run_automation_for_event
from .models import Automation, CustomField, Domain, Event, IntegrationConnection, Membership, Workspace
from .rbac import can_edit, require_role
from .views import (
    _automation_pipelines,
    _automation_trigger_data,
    _editor_trigger_choices,
    _facebook_apply_form_map,
    _facebook_body_key_for_token,
    _facebook_build_automation,
    _facebook_destino_needs,
    _facebook_resolve_new_fields,
    _facebook_suggest_target,
    _facebook_target_catalog,
    _parse_automation_canvas,
)


class _FakeRequest:
    """Minimal stand-in carrying the attribute the RBAC helpers read."""

    def __init__(self, membership):
        self.membership = membership


class RoleHierarchyTests(SimpleTestCase):
    """`Membership.can` / `.level` are pure logic — no database needed."""

    def test_levels_are_ordered(self):
        owner = Membership(role=Membership.ROLE_OWNER)
        viewer = Membership(role=Membership.ROLE_VIEWER)
        self.assertGreater(owner.level, viewer.level)

    def test_can_respects_the_hierarchy(self):
        admin = Membership(role=Membership.ROLE_ADMIN)
        self.assertTrue(admin.can(Membership.ROLE_MEMBER))
        self.assertTrue(admin.can(Membership.ROLE_ADMIN))
        self.assertFalse(admin.can(Membership.ROLE_OWNER))

    def test_member_cannot_act_as_admin(self):
        member = Membership(role=Membership.ROLE_MEMBER)
        self.assertTrue(member.can(Membership.ROLE_MEMBER))
        self.assertFalse(member.can(Membership.ROLE_ADMIN))


class RbacHelperTests(SimpleTestCase):
    def test_can_edit_allows_member_and_above(self):
        self.assertTrue(can_edit(_FakeRequest(Membership(role=Membership.ROLE_MEMBER))))
        self.assertTrue(can_edit(_FakeRequest(Membership(role=Membership.ROLE_OWNER))))

    def test_can_edit_blocks_viewers_and_anonymous(self):
        self.assertFalse(can_edit(_FakeRequest(Membership(role=Membership.ROLE_VIEWER))))
        self.assertFalse(can_edit(_FakeRequest(None)))

    def test_require_role_forbids_below_minimum(self):
        @require_role(Membership.ROLE_ADMIN)
        def view(request):
            return HttpResponse("ok")

        allowed = view(_FakeRequest(Membership(role=Membership.ROLE_ADMIN)))
        denied = view(_FakeRequest(Membership(role=Membership.ROLE_MEMBER)))
        missing = view(_FakeRequest(None))

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(missing.status_code, 403)


class SchemaIsolationTests(TransactionTestCase):
    """Two real tenant schemas; data in one must never surface in the other.

    Uses TransactionTestCase (not TestCase) because creating a workspace runs the
    tenant migrations as schema DDL, which can't live inside the test's atomic
    wrapper.
    """

    def setUp(self):
        connection.set_schema_to_public()
        self.ws_a = Workspace(schema_name="test_iso_a", name="Alpha")
        self.ws_a.save()
        Domain.objects.create(tenant=self.ws_a, domain="a.iso.test")
        self.ws_b = Workspace(schema_name="test_iso_b", name="Bravo")
        self.ws_b.save()
        Domain.objects.create(tenant=self.ws_b, domain="b.iso.test")

    def tearDown(self):
        connection.set_schema_to_public()
        for ws in (self.ws_a, self.ws_b):
            try:
                ws.delete()  # drops the schema + removes the public-side rows
            except Exception:
                pass

    def _schema_exists(self, name):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                [name],
            )
            return cur.fetchone() is not None

    def test_records_are_isolated_between_schemas(self):
        with tenant_context(self.ws_a):
            Company.objects.create(workspace=self.ws_a, name="Acme")
        with tenant_context(self.ws_b):
            Company.objects.create(workspace=self.ws_b, name="Globex")

        # Even the UNSCOPED manager only sees the current schema's rows, so this
        # proves the boundary is the schema itself, not just the manager filter.
        with tenant_context(self.ws_a):
            self.assertEqual(
                list(Company.all_objects.values_list("name", flat=True)), ["Acme"]
            )
        with tenant_context(self.ws_b):
            self.assertEqual(
                list(Company.all_objects.values_list("name", flat=True)), ["Globex"]
            )

    def test_deleting_a_workspace_drops_its_schema(self):
        self.assertTrue(self._schema_exists("test_iso_a"))

        self.ws_a.delete()

        self.assertFalse(self._schema_exists("test_iso_a"))
        # The other tenant is untouched.
        self.assertTrue(self._schema_exists("test_iso_b"))


class LeadValueCoercionTests(SimpleTestCase):
    """Pure logic: normalizing lead field names and typing their values."""

    def test_norm_key_is_accent_and_case_insensitive(self):
        self.assertEqual(_norm_key("Orçamento"), "orcamento")
        self.assertEqual(_norm_key("Phone Number"), "phone_number")
        self.assertEqual(_norm_key("e-mail"), "e_mail")

    def test_number_field_is_parsed(self):
        field = SimpleNamespace(field_type="number")
        self.assertEqual(_coerce_custom_value(field, "5000"), 5000)
        self.assertEqual(_coerce_custom_value(field, "5.5"), 5.5)
        # unparseable stays as the raw string (never crashes)
        self.assertEqual(_coerce_custom_value(field, "sob consulta"), "sob consulta")

    def test_checkbox_field_reads_truthy_words(self):
        field = SimpleNamespace(field_type="checkbox")
        self.assertTrue(_coerce_custom_value(field, "Sim"))
        self.assertTrue(_coerce_custom_value(field, "1"))
        self.assertFalse(_coerce_custom_value(field, "não"))

    def test_multiselect_field_splits_into_a_list(self):
        field = SimpleNamespace(field_type="multiselect")
        self.assertEqual(_coerce_custom_value(field, "a, b; c"), ["a", "b", "c"])
        self.assertEqual(_coerce_custom_value(field, ["x", "y"]), ["x", "y"])


class LeadCustomFieldCaptureTests(TransactionTestCase):
    """A Facebook/webhook lead must carry its extra answers into the contact:
    mapped to defined CustomFields (typed), and kept raw otherwise so no answer
    from the form is ever dropped."""

    def setUp(self):
        connection.set_schema_to_public()
        self.ws = Workspace(schema_name="test_lead_cap", name="LeadCap")
        self.ws.save()
        Domain.objects.create(tenant=self.ws, domain="leadcap.test")
        CustomField.objects.create(
            workspace=self.ws, object_type="contact",
            key="orcamento", label="Orçamento", field_type="number",
        )
        CustomField.objects.create(
            workspace=self.ws, object_type="contact",
            key="interesse", label="Interesse", field_type="text",
        )

    def tearDown(self):
        connection.set_schema_to_public()
        try:
            self.ws.delete()
        except Exception:
            pass

    def test_lead_extra_answers_land_on_the_contact(self):
        auto = SimpleNamespace(workspace=self.ws)
        event = SimpleNamespace(payload={"body": {
            "name": "Maria Silva",
            "email": "maria@example.com",
            "phone_number": "+5511999998888",
            "Orçamento": "5000",            # matches CustomField by label -> number
            "interesse": "Plano Premium",    # matches CustomField by key
            "origem_campanha": "verao2026",  # no field defined -> kept raw
        }})
        with tenant_context(self.ws):
            contact = _create_contact(auto, None, {}, event)
            contact.refresh_from_db()
            custom = dict(contact.custom or {})

        # standard fields mapped as before
        self.assertEqual(contact.email, "maria@example.com")
        self.assertEqual(contact.first_name, "Maria")
        # defined custom fields filled and typed
        self.assertEqual(custom.get("orcamento"), 5000)
        self.assertEqual(custom.get("interesse"), "Plano Premium")
        # unknown answer kept raw — nothing lost
        self.assertEqual(custom.get("origem_campanha"), "verao2026")
        # standard fields are NOT duplicated into custom
        self.assertNotIn("email", custom)
        self.assertNotIn("name", custom)


class FacebookMappingLogicTests(SimpleTestCase):
    """Pure logic for the per-form field mapping — no network, no DB."""

    def test_body_key_resolves_every_token_kind(self):
        self.assertEqual(_facebook_body_key_for_token("contact:name"), "name")
        self.assertEqual(_facebook_body_key_for_token("contact:email"), "email")
        self.assertEqual(_facebook_body_key_for_token("contact:phone"), "phone")
        self.assertEqual(_facebook_body_key_for_token("company:name"), "company")
        self.assertEqual(_facebook_body_key_for_token("deal:title"), "title")
        self.assertEqual(_facebook_body_key_for_token("custom:deal:orcamento"), "orcamento")
        self.assertIsNone(_facebook_body_key_for_token("ignore"))
        self.assertIsNone(_facebook_body_key_for_token(""))
        self.assertIsNone(_facebook_body_key_for_token("custom:contact:"))

    def test_suggest_matches_standard_questions_without_db(self):
        # keys/labels present in the suggestion table resolve before any DB query
        self.assertEqual(_facebook_suggest_target({"key": "email"}, None), "contact:email")
        self.assertEqual(_facebook_suggest_target({"key": "phone_number"}, None), "contact:phone")
        self.assertEqual(_facebook_suggest_target({"key": "full_name"}, None), "contact:name")
        self.assertEqual(_facebook_suggest_target({"key": "", "label": "Telefone"}, None), "contact:phone")

    def test_apply_form_map_routes_ignores_and_keeps_extras(self):
        conn = SimpleNamespace(config={"form_maps": {"F1": {
            "full_name": "contact:name",
            "email": "contact:email",
            "phone_number": "contact:phone",
            "qual_orcamento": "custom:deal:orcamento",
            "extra_q": "ignore",
        }}})
        flat = {
            "full_name": "Maria Silva",
            "email": "maria@example.com",
            "phone_number": "+5511999998888",
            "qual_orcamento": "5000",
            "extra_q": "descartar isso",
            "nova_pergunta": "surpresa",  # not in the map -> kept raw
        }
        body = _facebook_apply_form_map(conn, "F1", flat)
        self.assertEqual(body.get("name"), "Maria Silva")
        self.assertEqual(body.get("email"), "maria@example.com")
        self.assertEqual(body.get("phone"), "+5511999998888")
        self.assertEqual(body.get("orcamento"), "5000")
        # mapped source keys are not left dangling
        self.assertNotIn("full_name", body)
        # ignored answer is dropped entirely
        self.assertNotIn("descartar isso", body.values())
        # unmapped question is preserved (nothing lost)
        self.assertEqual(body.get("nova_pergunta"), "surpresa")

    def test_apply_form_map_is_a_noop_without_a_saved_map(self):
        conn = SimpleNamespace(config={})
        flat = {"email": "a@b.com", "phone_number": "123"}
        self.assertEqual(_facebook_apply_form_map(conn, "F1", flat), flat)

    def test_trigger_form_id_is_read_from_the_canvas(self):
        auto = SimpleNamespace(canvas={"nodes": [
            {"type": "trigger", "data": {"trigger": "facebook_lead", "trigger_form_id": "F1"}},
        ]})
        self.assertEqual(_automation_trigger_data(auto).get("trigger_form_id"), "F1")


class FacebookMappingCatalogTests(TransactionTestCase):
    """Suggestions and the target catalog resolve the workspace's custom fields."""

    def setUp(self):
        connection.set_schema_to_public()
        self.ws = Workspace(schema_name="test_fb_map", name="FbMap")
        self.ws.save()
        Domain.objects.create(tenant=self.ws, domain="fbmap.test")
        CustomField.objects.create(
            workspace=self.ws, object_type="deal",
            key="orcamento", label="Orçamento", field_type="number",
        )

    def tearDown(self):
        connection.set_schema_to_public()
        try:
            self.ws.delete()
        except Exception:
            pass

    def test_suggest_matches_a_custom_field_then_defaults_to_ignore(self):
        self.assertEqual(_facebook_suggest_target({"key": "orcamento"}, self.ws), "custom:deal:orcamento")
        self.assertEqual(_facebook_suggest_target({"label": "Orçamento"}, self.ws), "custom:deal:orcamento")
        self.assertEqual(_facebook_suggest_target({"key": "pergunta_solta"}, self.ws), "ignore")

    def test_suggest_can_still_create_new_field_when_allowed(self):
        self.assertEqual(
            _facebook_suggest_target({"key": "pergunta_solta"}, self.ws, allow_new_fields=True),
            "new:deal",
        )

    def test_catalog_lists_the_custom_field_in_its_object_group(self):
        catalog = _facebook_target_catalog(self.ws)
        deal_group = next(group for group in catalog if group["object"] == "deal")
        tokens = [token for token, _label in deal_group["options"]]
        self.assertIn("deal:title", tokens)
        self.assertIn("custom:deal:orcamento", tokens)

    def test_new_field_token_creates_a_custom_field_on_the_fly(self):
        resolved = _facebook_resolve_new_fields(self.ws, {
            "tipo de veículo": "new:deal",
            "email": "contact:email",
            "lixo": "new:bogus",
        })
        self.assertEqual(resolved["tipo de veículo"], "custom:deal:tipo-de-veiculo")
        self.assertEqual(resolved["email"], "contact:email")   # untouched
        self.assertEqual(resolved["lixo"], "ignore")            # unknown object
        self.assertTrue(CustomField.objects.filter(
            workspace=self.ws, object_type="deal", key="tipo-de-veiculo").exists())

    def test_new_field_token_is_ignored_when_creation_is_blocked(self):
        resolved = _facebook_resolve_new_fields(
            self.ws,
            {"tipo de veículo": "new:deal", "email": "contact:email"},
            allow_new_fields=False,
        )
        self.assertEqual(resolved["tipo de veículo"], "ignore")
        self.assertEqual(resolved["email"], "contact:email")


class FacebookDestinoNeedsTests(SimpleTestCase):
    """A mapping that targets an object field implies that object must be created."""

    def test_deal_and_company_fields_force_their_objects(self):
        needs = _facebook_destino_needs({
            "q1": "contact:email",
            "q2": "custom:deal:orcamento",
            "q3": "company:name",
        })
        self.assertTrue(needs["contact"])
        self.assertTrue(needs["deal"])
        self.assertTrue(needs["company"])

    def test_contact_only_mapping_forces_contact(self):
        needs = _facebook_destino_needs({"q1": "contact:email", "q2": "ignore"})
        self.assertTrue(needs["contact"])
        self.assertFalse(needs["deal"])
        self.assertFalse(needs["company"])


class EditorTriggerChoicesTests(SimpleTestCase):
    """The canvas hides 'Lead do Facebook' (owned by the Formulários screen) unless
    you're editing a flow that already uses it."""

    def test_facebook_hidden_for_new_automations(self):
        values = [value for value, _label in _editor_trigger_choices(None)]
        self.assertNotIn("facebook_lead", values)
        self.assertIn("contact_created", values)
        self.assertIn("webhook_received", values)

    def test_facebook_kept_when_editing_a_facebook_flow(self):
        values = [value for value, _label in _editor_trigger_choices("facebook_lead")]
        self.assertIn("facebook_lead", values)

    def test_editing_other_trigger_still_hides_facebook(self):
        values = [value for value, _label in _editor_trigger_choices("contact_created")]
        self.assertNotIn("facebook_lead", values)


class FacebookFlowGeneratorTests(TransactionTestCase):
    """The generated automation must actually run: a Facebook lead should create a
    contact + a deal in the chosen pipeline, with the mapped custom field filled —
    and re-saving the same form must not spawn a second automation."""

    def setUp(self):
        connection.set_schema_to_public()
        self.ws = Workspace(schema_name="test_fb_flow", name="FbFlow")
        self.ws.save()
        Domain.objects.create(tenant=self.ws, domain="fbflow.test")
        CustomField.objects.create(
            workspace=self.ws, object_type="deal",
            key="orcamento", label="Orçamento", field_type="number",
        )
        self.conn = IntegrationConnection.objects.create(
            workspace=self.ws, provider="facebook", name="Facebook Lead Ads",
            status="connected", config={"page_id": "P1", "page_access_token": "t"},
        )

    def tearDown(self):
        connection.set_schema_to_public()
        try:
            self.ws.delete()
        except Exception:
            pass

    def _destino(self):
        with tenant_context(self.ws):
            pipeline = _automation_pipelines(self.ws)[0]
            stage = pipeline.stages.first().key
        return {"create_contact": True, "create_deal": True, "pipeline": str(pipeline.pk), "stage": stage}, pipeline

    def test_generated_flow_creates_contact_and_deal_with_custom(self):
        destino, pipeline = self._destino()
        auto = _facebook_build_automation(self.ws, self.conn, "F1", "Forms Qualify-01", destino)
        self.assertTrue(auto.active)

        with tenant_context(self.ws):
            event = Event.objects.create(
                workspace=self.ws, event_type="facebook_lead", object_repr="Lead",
                payload={"body": {
                    "name": "Maria Silva",
                    "email": "maria@example.com",
                    "phone": "+5511999998888",
                    "orcamento": "5000",  # as the form map would have renamed it
                }, "form_id": "F1"},
            )
            run_automation_for_event(auto, event, None)
            contact = Contact.all_objects.get(email="maria@example.com")
            deal = Deal.all_objects.filter(pipeline=pipeline).first()

        self.assertEqual(contact.first_name, "Maria")
        self.assertIsNotNone(deal)
        self.assertEqual(deal.contact_id, contact.id)
        self.assertEqual(dict(deal.custom or {}).get("orcamento"), 5000)

    def test_regenerating_updates_the_same_automation(self):
        destino, _ = self._destino()
        auto1 = _facebook_build_automation(self.ws, self.conn, "F1", "Forms Qualify-01", destino)
        auto2 = _facebook_build_automation(self.ws, self.conn, "F1", "Forms Qualify-01 (novo nome)", destino)
        self.assertEqual(auto1.pk, auto2.pk)
        self.assertEqual(Automation.objects.filter(workspace=self.ws).count(), 1)

    def test_generated_canvas_survives_a_parse_roundtrip(self):
        destino, _ = self._destino()
        auto = _facebook_build_automation(self.ws, self.conn, "F1", "Forms Qualify-01", destino)
        parsed = _parse_automation_canvas(SimpleNamespace(POST={"canvas": json.dumps(auto.canvas)}))
        trigger = next(n for n in parsed["nodes"] if n["type"] == "trigger")
        self.assertEqual(trigger["data"].get("trigger_form_id"), "F1")
        self.assertEqual(len([n for n in parsed["nodes"] if n["type"] == "action"]), 2)
        self.assertEqual(len(parsed["edges"]), 2)

    def test_no_objects_selected_pauses_the_flow(self):
        auto = _facebook_build_automation(
            self.ws, self.conn, "F2", "Vazio",
            {"create_contact": False, "create_company": False, "create_deal": False},
        )
        self.assertFalse(auto.active)
