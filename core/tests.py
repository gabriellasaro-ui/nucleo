"""Core tests: role-based access control and multi-tenant schema isolation.

The isolation tests are the important ones — they prove that data written in one
workspace's Postgres schema is invisible from another, which is the whole safety
promise of the schema-per-tenant design.
"""
from types import SimpleNamespace

from django.db import connection
from django.http import HttpResponse
from django.test import SimpleTestCase, TransactionTestCase
from django_tenants.utils import tenant_context

from modules.crm.models import Company

from .events import _coerce_custom_value, _create_contact, _norm_key
from .models import CustomField, Domain, Membership, Workspace
from .rbac import can_edit, require_role
from .views import (
    _automation_trigger_data,
    _facebook_apply_form_map,
    _facebook_body_key_for_token,
    _facebook_suggest_target,
    _facebook_target_catalog,
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

    def test_suggest_matches_a_custom_field_then_falls_back_to_ignore(self):
        self.assertEqual(_facebook_suggest_target({"key": "orcamento"}, self.ws), "custom:deal:orcamento")
        self.assertEqual(_facebook_suggest_target({"label": "Orçamento"}, self.ws), "custom:deal:orcamento")
        self.assertEqual(_facebook_suggest_target({"key": "pergunta_solta"}, self.ws), "ignore")

    def test_catalog_lists_the_custom_field_in_its_object_group(self):
        catalog = _facebook_target_catalog(self.ws)
        deal_group = next(group for group in catalog if group["object"] == "deal")
        tokens = [token for token, _label in deal_group["options"]]
        self.assertIn("deal:title", tokens)
        self.assertIn("custom:deal:orcamento", tokens)
