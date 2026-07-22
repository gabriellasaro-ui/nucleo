"""Core tests: role-based access control and multi-tenant schema isolation.

The isolation tests are the important ones — they prove that data written in one
workspace's Postgres schema is invisible from another, which is the whole safety
promise of the schema-per-tenant design.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db import connection
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django_tenants.utils import tenant_context

from modules.crm.models import Company, Contact, Deal

from .events import _coerce_custom_value, _create_contact, _norm_key, run_automation_for_event
from .models import Automation, CustomField, Domain, Event, IntegrationConnection, Membership, Workspace
from .rbac import can_edit, require_role
from .whatsapp_service import connect_instance, create_instance
from .views import (
    _automation_pipelines,
    _automation_trigger_data,
    _editor_trigger_choices,
    _fixed_custom_fields,
    _facebook_apply_form_map,
    _facebook_attribution_values,
    _facebook_body_key_for_token,
    _facebook_build_automation,
    _facebook_candidate_map,
    _facebook_destino_needs,
    _facebook_ensure_attribution_fields,
    _facebook_form_accepts_leads,
    _facebook_question_requires_mapping,
    _facebook_resolve_new_fields,
    _facebook_set_form_enabled,
    _facebook_suggest_target,
    _facebook_target_catalog,
    _normalize_facebook_leadgen,
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


class EvoGoClientTests(SimpleTestCase):
    @override_settings(
        EVOGO_API_URL="https://evogo.example.test",
        EVOGO_GLOBAL_API_KEY="global-secret",
    )
    @patch("core.whatsapp_service.urllib.request.urlopen")
    def test_instance_uses_admin_key_then_workspace_token(self, urlopen):
        create_response = MagicMock()
        create_response.read.return_value = json.dumps({
            "message": "success",
            "data": {"id": "INSTANCE-1", "name": "nucleo-1"},
        }).encode()
        connect_response = MagicMock()
        connect_response.read.return_value = json.dumps({
            "message": "success", "data": {},
        }).encode()
        urlopen.return_value.__enter__.side_effect = [
            create_response,
            connect_response,
        ]

        instance, token = create_instance("nucleo-1")
        connect_instance(token, "https://crm.example.test/webhooks/whatsapp/secret/")

        create_request = urlopen.call_args_list[0].args[0]
        connect_request = urlopen.call_args_list[1].args[0]
        create_payload = json.loads(create_request.data)
        connect_payload = json.loads(connect_request.data)
        self.assertEqual(instance["id"], "INSTANCE-1")
        self.assertNotEqual(token, "global-secret")
        self.assertEqual(create_request.get_header("Apikey"), "global-secret")
        self.assertEqual(connect_request.get_header("Apikey"), token)
        self.assertFalse(create_payload["advancedSettings"]["readMessages"])
        self.assertTrue(create_payload["advancedSettings"]["ignoreGroups"])
        self.assertIn("MESSAGE", connect_payload["subscribe"])
        self.assertIn("HISTORY_SYNC", connect_payload["subscribe"])


class WhatsAppTemplateTests(SimpleTestCase):
    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_connected_inbox_renders_conversation_and_composer(self):
        conversation = SimpleNamespace(
            pk=7,
            name="Lead do formulário",
            phone="5511999991234",
            last_message="Tenho interesse",
            last_message_at=None,
            unread_count=1,
            contact=None,
        )
        message = SimpleNamespace(
            direction="incoming",
            text="Tenho interesse",
            sent_at=None,
            status="received",
        )
        html = render_to_string("core/whatsapp.html", {
            "whatsapp_available": True,
            "whatsapp_connected": True,
            "whatsapp_pairing": False,
            "whatsapp_status": {"Name": "Comercial"},
            "whatsapp_config": {"instance_token": "hidden"},
            "whatsapp_error": "",
            "conversations": [conversation],
            "selected_conversation": conversation,
            "draft_contact": None,
            "thread_messages": [message],
            "contact_options": [],
            "conversation_search": "",
            "can_admin": True,
            "can_edit": True,
        })

        self.assertIn("Lead do formulário", html)
        self.assertIn("Tenho interesse", html)
        self.assertIn('action="/whatsapp/send/"', html)
        self.assertIn("Desconectar", html)
        self.assertNotIn("instance_token", html)


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
            "utm_source": "facebook",
            "utm_campaign": "Campanha Julho",
        }})
        with tenant_context(self.ws):
            _facebook_ensure_attribution_fields(self.ws)
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
        # attribution fields belong to Deal and must not leak into Contact
        self.assertNotIn("utm_source", custom)
        self.assertNotIn("utm_campaign", custom)
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

    def test_meta_campaign_metadata_becomes_deal_utm_values(self):
        values = _facebook_attribution_values({
            "platform": "ig",
            "campaign_name": "Campanha Julho",
            "adset_name": "Publico SP",
            "ad_name": "Criativo A",
            "is_organic": False,
        })
        self.assertEqual(values, {
            "utm_source": "instagram",
            "utm_medium": "paid_social",
            "utm_campaign": "Campanha Julho",
            "utm_content": "Criativo A",
            "utm_term": "Publico SP",
        })

    def test_inline_meta_lead_keeps_answers_and_attribution(self):
        data = _normalize_facebook_leadgen({
            "field_data": [{"name": "full_name", "values": ["Maria Silva"]}],
            "campaign_id": "C1",
            "adset_id": "S1",
            "ad_id": "A1",
        }, None)
        self.assertEqual(data["name"], "Maria Silva")
        self.assertEqual(data["utm_source"], "facebook")
        self.assertEqual(data["utm_campaign"], "C1")
        self.assertEqual(data["utm_term"], "S1")
        self.assertEqual(data["utm_content"], "A1")

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

    def test_each_form_uses_only_its_own_saved_mapping(self):
        conn = SimpleNamespace(config={"form_maps": {
            "FORM-A": {"pergunta": "custom:deal:campo-form-a"},
            "FORM-B": {"pergunta": "custom:deal:campo-form-b"},
        }})

        self.assertEqual(
            _facebook_apply_form_map(conn, "FORM-A", {"pergunta": "A"}),
            {"campo-form-a": "A"},
        )
        self.assertEqual(
            _facebook_apply_form_map(conn, "FORM-B", {"pergunta": "B"}),
            {"campo-form-b": "B"},
        )

    def test_trigger_form_id_is_read_from_the_canvas(self):
        auto = SimpleNamespace(canvas={"nodes": [
            {"type": "trigger", "data": {"trigger": "facebook_lead", "trigger_form_id": "F1"}},
        ]})
        self.assertEqual(_automation_trigger_data(auto).get("trigger_form_id"), "F1")

    def test_native_custom_questions_require_a_destination(self):
        self.assertTrue(_facebook_question_requires_mapping({
            "key": "qual_orcamento", "label": "Qual o orçamento?", "type": "CUSTOM",
        }))
        self.assertTrue(_facebook_question_requires_mapping({
            "key": "qual_produto", "label": "Qual produto?", "type": "",
        }))
        self.assertFalse(_facebook_question_requires_mapping({
            "key": "email", "label": "Email", "type": "EMAIL",
        }))

    def test_custom_questions_cannot_be_sent_to_fixed_crm_fields(self):
        questions = [
            {"key": "tipo", "label": "Qual o tipo?", "type": "CUSTOM"},
            {"key": "email", "label": "Email", "type": "EMAIL"},
        ]
        candidate = _facebook_candidate_map(
            "FORM-A",
            questions,
            {
                "tipo": "custom:deal:utm-source",
                "email": "contact:email",
            },
            {"custom:deal:utm-source", "contact:email", "new:deal"},
        )

        self.assertEqual(candidate["tipo"], "new:deal")
        self.assertEqual(candidate["email"], "contact:email")

        saved = _facebook_candidate_map(
            "FORM-A",
            questions[:1],
            {"tipo": "custom:deal:fb-form-a-tipo"},
            {"custom:deal:fb-form-a-tipo", "new:deal"},
        )
        self.assertEqual(saved["tipo"], "custom:deal:fb-form-a-tipo")

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_mapping_template_keeps_standard_fields_compact(self):
        catalog = [
            {"object": "contact", "label": "Contato", "options": [("contact:name", "Nome completo")]},
            {"object": "company", "label": "Empresa", "options": []},
            {"object": "deal", "label": "Negócio", "options": [("custom:deal:teste", "Teste")]},
        ]
        rows = [
            {
                "key": "nome", "label": "Full name", "current": "contact:name",
                "current_object": "contact", "current_object_label": "Contato",
                "current_field_label": "Nome completo", "required_mapping": False,
                "invalid_mapping": False,
            },
            {
                "key": "tipo", "label": "Para qual tipo de caminhão?", "current": "",
                "current_object": "", "current_object_label": "",
                "current_field_label": "", "required_mapping": True,
                "invalid_mapping": False,
            },
        ]
        html = render_to_string("core/facebook_form_map.html", {
            "form_id": "F1",
            "form_name": "Forms Qualify - 01",
            "has_questions": True,
            "rows": rows,
            "catalog": catalog,
            "pipelines": [],
            "dest": {"create_contact": True, "create_deal": True},
            "has_custom_fields": True,
            "allow_new_fields": True,
            "flow_pk": 1,
            "form_can_toggle": True,
            "form_flow_active": True,
        })

        self.assertIn("Contato / Nome completo", html)
        self.assertIn('data-map-required="true"', html)
        self.assertNotIn("fb-map-summary", html)
        self.assertNotIn("Editar no modo avançado", html)
        self.assertNotIn("+ Campo novo", html)
        self.assertIn("Campo automático", html)
        self.assertIn("data-generated-target", html)
        self.assertNotIn("Usar outro campo do CRM", html)
        self.assertNotIn("data-reuse-field", html)
        self.assertNotIn("fb-map-section__action", html)
        self.assertIn("Desativar formulário", html)

    def test_form_enabled_flag_is_backward_compatible_and_explicit(self):
        self.assertTrue(_facebook_form_accepts_leads({}, "F1"))
        self.assertTrue(_facebook_form_accepts_leads({"form_enabled": {"F1": True}}, "F1"))
        self.assertFalse(_facebook_form_accepts_leads({"form_enabled": {"F1": False}}, "F1"))

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_forms_template_shows_the_correct_toggle_action(self):
        html = render_to_string("core/facebook_forms.html", {
            "page_name": "Pagina teste",
            "forms": [
                {
                    "id": "ACTIVE", "name": "Formulario ativo", "mapped": True,
                    "flow_active": True, "can_toggle": True,
                },
                {
                    "id": "PAUSED", "name": "Formulario pausado", "mapped": True,
                    "flow_active": False, "can_toggle": True,
                },
                {
                    "id": "NEW", "name": "Formulario novo", "mapped": False,
                    "flow_active": False, "can_toggle": False,
                },
            ],
        })

        self.assertIn('name="active" value="0"', html)
        self.assertIn('name="active" value="1"', html)
        self.assertIn("Desativar", html)
        self.assertIn("Ativar", html)
        self.assertEqual(html.count("facebook/forms/NEW/toggle/"), 0)


class FacebookMappingCatalogTests(TransactionTestCase):
    """Fixed CRM fields stay separate from form-owned Meta question fields."""

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

    def test_suggest_never_consumes_a_fixed_custom_field(self):
        self.assertEqual(
            _facebook_suggest_target(
                {"key": "orcamento"}, self.ws, allow_new_fields=False,
            ),
            "ignore",
        )
        self.assertEqual(
            _facebook_suggest_target(
                {"label": "Orçamento"}, self.ws, allow_new_fields=True,
            ),
            "new:deal",
        )

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

    def test_catalog_can_be_scoped_to_the_current_form(self):
        unlinked = _facebook_target_catalog(self.ws, custom_tokens=set())
        deal_group = next(group for group in unlinked if group["object"] == "deal")
        tokens = [token for token, _label in deal_group["options"]]
        self.assertIn("deal:title", tokens)
        self.assertNotIn("custom:deal:orcamento", tokens)

        linked = _facebook_target_catalog(
            self.ws, custom_tokens={"custom:deal:orcamento"},
        )
        deal_group = next(group for group in linked if group["object"] == "deal")
        tokens = [token for token, _label in deal_group["options"]]
        self.assertIn("custom:deal:orcamento", tokens)

    def test_linked_fields_do_not_change_native_question_suggestions(self):
        self.assertEqual(
            _facebook_suggest_target(
                {"key": "orcamento"},
                self.ws,
                allow_new_fields=False,
                allowed_custom_tokens={"custom:deal:orcamento"},
            ),
            "ignore",
        )

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

    def test_generated_fields_are_isolated_by_meta_form(self):
        question = {"tipo_de_veiculo": "Para qual tipo de caminhão?"}
        first = _facebook_resolve_new_fields(
            self.ws,
            {"tipo_de_veiculo": "new:deal"},
            form_id="FORM-A",
            question_labels=question,
        )
        second = _facebook_resolve_new_fields(
            self.ws,
            {"tipo_de_veiculo": "new:deal"},
            form_id="FORM-B",
            question_labels=question,
        )

        self.assertNotEqual(first["tipo_de_veiculo"], second["tipo_de_veiculo"])
        self.assertEqual(first["tipo_de_veiculo"], "custom:deal:fb-form-a-tipo-de-veiculo")
        self.assertEqual(second["tipo_de_veiculo"], "custom:deal:fb-form-b-tipo-de-veiculo")
        self.assertEqual(
            CustomField.objects.get(
                workspace=self.ws, object_type="deal", key="fb-form-a-tipo-de-veiculo",
            ).label,
            "Para qual tipo de caminhão?",
        )

    def test_generated_form_fields_do_not_become_fixed_crm_fields(self):
        _facebook_resolve_new_fields(
            self.ws,
            {"tipo": "new:deal"},
            form_id="FORM-A",
            question_labels={"tipo": "Qual o tipo?"},
        )

        keys = set(_fixed_custom_fields(self.ws).values_list("key", flat=True))
        self.assertIn("orcamento", keys)
        self.assertNotIn("fb-form-a-tipo", keys)

    def test_utm_fields_are_fixed_exclusively_on_deals(self):
        fields = _facebook_ensure_attribution_fields(self.ws)
        self.assertEqual(
            {field.key for field in fields},
            {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"},
        )
        self.assertTrue(all(field.object_type == "deal" for field in fields))
        self.assertFalse(CustomField.objects.filter(
            workspace=self.ws,
            object_type="contact",
            key__startswith="utm_",
        ).exists())
        _facebook_ensure_attribution_fields(self.ws)
        self.assertEqual(CustomField.objects.filter(
            workspace=self.ws,
            object_type="deal",
            key__startswith="utm_",
        ).count(), 5)

    def test_new_field_token_is_ignored_when_creation_is_blocked(self):
        resolved = _facebook_resolve_new_fields(
            self.ws,
            {"tipo de veículo": "new:deal", "email": "contact:email"},
            allow_new_fields=False,
        )
        self.assertEqual(resolved["tipo de veículo"], "ignore")
        self.assertEqual(resolved["email"], "contact:email")

    def test_disabling_a_form_preserves_its_mapping_and_can_be_reversed(self):
        auto = Automation.objects.create(
            workspace=self.ws,
            name="Facebook - Form A",
            trigger="facebook_lead",
            action="create_deal",
            actions=[{"type": "create_deal"}],
            active=True,
        )
        conn = IntegrationConnection.objects.create(
            workspace=self.ws,
            provider="facebook",
            name="Facebook",
            status="connected",
            config={
                "forms": [{"id": "FORM-A", "name": "Form A"}],
                "form_maps": {"FORM-A": {"email": "contact:email"}},
                "form_dest": {"FORM-A": {"create_contact": True, "create_deal": True}},
                "form_flows": {"FORM-A": auto.pk},
            },
        )

        _facebook_set_form_enabled(self.ws, conn, "FORM-A", False)
        conn.refresh_from_db()
        auto.refresh_from_db()
        self.assertFalse(conn.config["form_enabled"]["FORM-A"])
        self.assertFalse(auto.active)
        self.assertEqual(conn.config["form_maps"]["FORM-A"]["email"], "contact:email")
        self.assertTrue(conn.config["form_dest"]["FORM-A"]["create_deal"])

        _facebook_set_form_enabled(self.ws, conn, "FORM-A", True)
        conn.refresh_from_db()
        auto.refresh_from_db()
        self.assertTrue(conn.config["form_enabled"]["FORM-A"])
        self.assertTrue(auto.active)
        self.assertEqual(Automation.objects.filter(workspace=self.ws).count(), 1)


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

    def test_pending_new_field_already_forces_its_object(self):
        needs = _facebook_destino_needs({"q1": "new:deal"})
        self.assertTrue(needs["deal"])


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
            _facebook_ensure_attribution_fields(self.ws)
            event = Event.objects.create(
                workspace=self.ws, event_type="facebook_lead", object_repr="Lead",
                payload={"body": {
                    "name": "Maria Silva",
                    "email": "maria@example.com",
                    "phone": "+5511999998888",
                    "orcamento": "5000",  # as the form map would have renamed it
                    "utm_source": "facebook",
                    "utm_medium": "paid_social",
                    "utm_campaign": "Campanha Julho",
                }, "form_id": "F1"},
            )
            run_automation_for_event(auto, event, None)
            contact = Contact.all_objects.get(email="maria@example.com")
            deal = Deal.all_objects.filter(pipeline=pipeline).first()

        self.assertEqual(contact.first_name, "Maria")
        self.assertIsNotNone(deal)
        self.assertEqual(deal.contact_id, contact.id)
        self.assertEqual(dict(deal.custom or {}).get("orcamento"), 5000)
        self.assertEqual(dict(deal.custom or {}).get("utm_source"), "facebook")
        self.assertEqual(dict(deal.custom or {}).get("utm_campaign"), "Campanha Julho")
        self.assertNotIn("utm_source", dict(contact.custom or {}))

    def test_regenerating_updates_the_same_automation(self):
        destino, _ = self._destino()
        auto1 = _facebook_build_automation(self.ws, self.conn, "F1", "Forms Qualify-01", destino)
        auto2 = _facebook_build_automation(self.ws, self.conn, "F1", "Forms Qualify-01 (novo nome)", destino)
        self.assertEqual(auto1.pk, auto2.pk)
        self.assertEqual(Automation.objects.filter(workspace=self.ws).count(), 1)

    def test_facebook_source_key_is_idempotent(self):
        with tenant_context(self.ws):
            first, first_created = Event.objects.get_or_create(
                workspace=self.ws,
                event_type="facebook_lead",
                source_key="facebook:P1:L1",
                defaults={"object_repr": "Lead 1"},
            )
            second, second_created = Event.objects.get_or_create(
                workspace=self.ws,
                event_type="facebook_lead",
                source_key="facebook:P1:L1",
                defaults={"object_repr": "Lead duplicado"},
            )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)

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
