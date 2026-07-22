"""Model-level tests for the CRM module.

These run inside a single disposable tenant schema (`TenantTestCase` creates the
`test` schema and points the connection at it), so every query here already lives
in an isolated workspace schema. Cross-tenant isolation itself is proved
separately in ``core.tests.SchemaIsolationTests``.
"""
import json

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django_tenants.test.cases import TenantTestCase

from core.context_processors import navigation
from core.events import _condition_matches
from core.forms import WhatsAppPromotionForm
from core.models import CustomField, Event, IntegrationConnection, Membership
from core.tenancy import clear_current_workspace, set_current_workspace
from core.views import whatsapp_promote_drawer, whatsapp_webhook
from core.whatsapp_inbox import (
    normalize_whatsapp_phone,
    promote_whatsapp_conversation,
    sync_whatsapp_contact_directory,
    whatsapp_unread_count,
)

from .models import (
    Company, Contact, Deal, Pipeline, Tag, WhatsAppConversation, WhatsAppMessage,
)
from .views import _pipeline_modal_context, deal_workspace


class TenantTestBase(TenantTestCase):
    """Give the auto-created test workspace a name (the field is required)."""

    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = "Tenant de Teste"


class WhatsAppInboxTests(TenantTestBase):
    def setUp(self):
        self.connection = IntegrationConnection.objects.create(
            workspace=self.tenant,
            provider="whatsapp",
            name="WhatsApp",
            status="disconnected",
            config={
                "instance_id": "INSTANCE-1",
                "instance_token": "instance-token",
                "webhook_secret": "webhook-secret",
            },
        )
        self.factory = RequestFactory()

    def _send_webhook(self, payload, secret="webhook-secret"):
        request = self.factory.post(
            f"/webhooks/whatsapp/{secret}/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        return whatsapp_webhook(request, secret)

    def test_form_contact_is_reused_and_webhook_is_idempotent(self):
        contact = Contact.objects.create(
            workspace=self.tenant,
            first_name="Lead",
            last_name="Formulario",
            phone="(11) 99999-1234",
            stage="lead",
        )
        payload = {
            "event": "Message",
            "instanceId": "INSTANCE-1",
            "instanceToken": "instance-token",
            "data": {
                "Info": {
                    "ID": "MESSAGE-1",
                    "Chat": "5511999991234@s.whatsapp.net",
                    "Sender": "5511999991234@s.whatsapp.net",
                    "PushName": "Lead Meta",
                    "IsFromMe": False,
                    "Timestamp": "2026-07-22T15:30:00-03:00",
                },
                "Message": {"conversation": "Quero saber mais"},
            },
        }

        first = self._send_webhook(payload)
        second = self._send_webhook(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        conversation = WhatsAppConversation.objects.get()
        self.assertEqual(conversation.contact_id, contact.pk)
        self.assertEqual(conversation.unread_count, 1)
        self.assertEqual(conversation.last_message, "Quero saber mais")
        self.assertEqual(WhatsAppMessage.objects.count(), 1)
        self.assertEqual(Contact.objects.count(), 1)

    def test_unknown_inbound_number_waits_for_manual_crm_promotion(self):
        payload = {
            "event": "Message",
            "instanceId": "INSTANCE-1",
            "instanceToken": "wrong-token",
            "data": {
                "Info": {
                    "ID": "MESSAGE-2",
                    "Chat": "5511888887777@s.whatsapp.net",
                    "PushName": "Pessoa Nova",
                    "IsFromMe": False,
                },
                "Message": {"extendedTextMessage": {"text": "Olá"}},
            },
        }
        denied = self._send_webhook(payload)
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(Contact.objects.exists())


        payload["instanceToken"] = "instance-token"
        accepted = self._send_webhook(payload)
        self.assertEqual(accepted.status_code, 200)
        conversation = WhatsAppConversation.objects.get()
        self.assertEqual(conversation.name, "Pessoa Nova")
        self.assertEqual(conversation.display_name, "+5511888887777")
        self.assertIsNone(conversation.contact)
        self.assertFalse(Contact.objects.exists())

    def test_history_sync_does_not_import_old_phone_conversations(self):
        payload = {
            "event": "HistorySync",
            "instanceId": "INSTANCE-1",
            "instanceToken": "instance-token",
            "data": {
                "Data": {
                    "Conversations": [{
                        "ID": "5511777776666@s.whatsapp.net",
                        "Messages": [{
                            "Message": {
                                "Key": {
                                    "ID": "HISTORY-1",
                                    "RemoteJID": "5511777776666@s.whatsapp.net",
                                    "FromMe": False,
                                },
                                "PushName": "Lead Antigo",
                                "MessageTimestamp": "1784752200",
                                "Message": {"conversation": "Mensagem anterior"},
                            },
                        }],
                    }],
                },
            },
        }

        response = self._send_webhook(payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(WhatsAppConversation.objects.exists())
        self.assertFalse(Contact.objects.exists())
        self.assertFalse(WhatsAppMessage.objects.exists())

    def test_lid_uses_phone_number_alternate_and_does_not_duplicate_chat(self):
        base = {
            "event": "Message",
            "instanceId": "INSTANCE-1",
            "instanceToken": "instance-token",
            "data": {
                "Info": {
                    "ID": "MESSAGE-LID-1",
                    "Chat": "227307469975622@lid",
                    "Sender": "227307469975622@lid",
                    "SenderAlt": "553173042273@s.whatsapp.net",
                    "PushName": "Gabriel",
                    "IsFromMe": False,
                },
                "Message": {"conversation": "Olá"},
            },
        }
        self.assertEqual(self._send_webhook(base).status_code, 200)
        base["data"]["Info"].update({
            "ID": "MESSAGE-LID-2",
            "Chat": "553173042273@s.whatsapp.net",
            "Sender": "553173042273@s.whatsapp.net",
        })
        self.assertEqual(self._send_webhook(base).status_code, 200)

        conversation = WhatsAppConversation.objects.get()
        self.assertEqual(conversation.phone, "553173042273")
        self.assertEqual(conversation.remote_jid, "553173042273@s.whatsapp.net")
        self.assertEqual(conversation.messages.count(), 2)
        self.assertFalse(Contact.objects.exists())

    def test_lid_without_phone_alternate_is_ignored(self):
        payload = {
            "event": "Message",
            "instanceId": "INSTANCE-1",
            "instanceToken": "instance-token",
            "data": {
                "Info": {
                    "ID": "MESSAGE-LID-ONLY",
                    "Chat": "227307469975622@lid",
                    "Sender": "227307469975622@lid",
                    "PushName": "Identidade interna",
                    "IsFromMe": False,
                },
                "Message": {"conversation": "Olá"},
            },
        }

        self.assertEqual(self._send_webhook(payload).status_code, 200)
        self.assertFalse(WhatsAppConversation.objects.exists())

    def test_manual_promotion_is_idempotent(self):
        conversation = WhatsAppConversation.objects.create(
            workspace=self.tenant,
            instance_id="INSTANCE-1",
            remote_jid="5511888887777@s.whatsapp.net",
            phone="5511888887777",
            name="Pessoa Nova",
        )

        first, first_created = promote_whatsapp_conversation(
            self.tenant, conversation,
        )
        second, second_created = promote_whatsapp_conversation(
            self.tenant, conversation,
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Contact.objects.count(), 1)
        conversation.refresh_from_db()
        self.assertEqual(conversation.contact_id, first.pk)

    def test_contact_directory_names_conversations_without_importing_the_phonebook(self):
        conversation = WhatsAppConversation.objects.create(
            workspace=self.tenant,
            instance_id="INSTANCE-1",
            remote_jid="5511777776666@s.whatsapp.net",
            phone="5511777776666",
            name="",
        )

        result = sync_whatsapp_contact_directory(
            self.tenant,
            "INSTANCE-1",
            [{
                "Jid": "5511777776666@s.whatsapp.net",
                "FullName": "",
                "BusinessName": "",
                "PushName": "Cliente Conhecido",
            }],
        )

        conversation.refresh_from_db()
        self.assertEqual(conversation.name, "Cliente Conhecido")
        self.assertEqual(result["conversation_updates"], 1)
        self.assertFalse(Contact.objects.exists())

    def test_sidebar_notification_counts_only_the_active_whatsapp_inbox(self):
        WhatsAppConversation.objects.create(
            workspace=self.tenant,
            instance_id="INSTANCE-1",
            remote_jid="5511999990001@s.whatsapp.net",
            phone="5511999990001",
            unread_count=4,
        )
        WhatsAppConversation.objects.create(
            workspace=self.tenant,
            instance_id="INSTANCE-1",
            remote_jid="5511999990002@s.whatsapp.net",
            phone="5511999990002",
            unread_count=20,
            is_history_import=True,
        )
        WhatsAppConversation.objects.create(
            workspace=self.tenant,
            instance_id="OLD-INSTANCE",
            remote_jid="5511999990003@s.whatsapp.net",
            phone="5511999990003",
            unread_count=30,
        )
        WhatsAppConversation.objects.create(
            workspace=self.tenant,
            instance_id="INSTANCE-1",
            remote_jid="123456789@lid",
            phone="123456789",
            unread_count=40,
        )

        self.assertEqual(whatsapp_unread_count(self.tenant), 4)
        request = self.factory.get("/")
        request.workspace = self.tenant
        request.membership = None
        context = navigation(request)
        whatsapp_item = next(
            item
            for section in context["nav_sections"]
            for item in section["items"]
            if item["label"] == "WhatsApp"
        )
        self.assertEqual(whatsapp_item["notification_count"], 4)
        self.assertEqual(whatsapp_item["notification_label"], "4")


class WhatsAppPromotionTests(TenantTestBase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="whatsapp-promotion-test",
            password="test-password",
        )
        self.membership = Membership.objects.create(
            user=self.user,
            workspace=self.tenant,
            role=Membership.ROLE_MEMBER,
        )
        IntegrationConnection.objects.create(
            workspace=self.tenant,
            provider="whatsapp",
            name="WhatsApp",
            status="connected",
            config={"instance_id": "INSTANCE-CRM"},
        )
        self.pipeline = Pipeline.objects.create(
            workspace=self.tenant,
            name="Comercial",
            is_default=True,
        )
        self.pipeline.stages.create(
            key="entrada",
            name="Entrada",
            kind="open",
            order=0,
        )
        self.other_pipeline = Pipeline.objects.create(
            workspace=self.tenant,
            name="Renovação",
        )
        self.other_pipeline.stages.create(
            key="renovacao",
            name="Renovação",
            kind="open",
            order=0,
        )
        self.conversation = WhatsAppConversation.objects.create(
            workspace=self.tenant,
            instance_id="INSTANCE-CRM",
            remote_jid="5511999990000@s.whatsapp.net",
            phone="5511999990000",
            name="Lead WhatsApp",
        )
        self.factory = RequestFactory()

    def _request(self, method="get", data=None, htmx=False):
        request = getattr(self.factory, method)(
            "/whatsapp/conversations/1/crm/",
            data=data or {},
            **({"HTTP_HX_REQUEST": "true"} if htmx else {}),
        )
        request.user = self.user
        request.workspace = self.tenant
        request.membership = self.membership
        return request

    def test_form_rejects_stage_from_another_pipeline(self):
        form = WhatsAppPromotionForm(
            data={
                "first_name": "Lead",
                "phone": "+5511999990000",
                "deal_title": "Venda pelo WhatsApp",
                "pipeline": self.pipeline.pk,
                "stage": "renovacao",
            },
            workspace=self.tenant,
            mode="contact_deal",
        )

        self.assertFalse(form.is_valid())
        self.assertIn("stage", form.errors)

    def test_promotion_creates_contact_and_deal_with_commercial_data(self):
        response = whatsapp_promote_drawer(
            self._request("post", {
                "mode": "contact_deal",
                "first_name": "Lead",
                "last_name": "Qualificado",
                "phone": "+5511999990000",
                "email": "lead@example.test",
                "deal_title": "Projeto via WhatsApp",
                "value": "12500.50",
                "pipeline": self.pipeline.pk,
                "stage": "entrada",
                "owner": self.user.pk,
            }, htmx=True),
            self.conversation.pk,
        )

        self.assertEqual(response.status_code, 204)
        contact = Contact.objects.get()
        deal = Deal.objects.get()
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.contact_id, contact.pk)
        self.assertEqual(deal.contact_id, contact.pk)
        self.assertEqual(deal.pipeline_id, self.pipeline.pk)
        self.assertEqual(deal.stage, "entrada")
        self.assertEqual(str(deal.value), "12500.50")

    def test_deal_workspace_shows_chat_and_rejects_wrong_pipeline_stage(self):
        contact = Contact.objects.create(
            workspace=self.tenant,
            first_name="Lead",
            phone="+5511999990000",
        )
        self.conversation.contact = contact
        self.conversation.save(update_fields=["contact"])
        deal = Deal.objects.create(
            workspace=self.tenant,
            title="Negócio aberto",
            value=100,
            pipeline=self.pipeline,
            stage="entrada",
            contact=contact,
            owner=self.user,
        )

        get_response = deal_workspace(self._request(), deal.pk)
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "<b>Lead</b>", html=True)
        self.assertNotContains(get_response, "Lead WhatsApp")
        self.assertContains(get_response, "Informações do negócio")

        post_response = deal_workspace(self._request("post", {
            "title": "Não deve salvar",
            "value": "500",
            "pipeline": self.other_pipeline.pk,
            "stage": "entrada",
            "contact": contact.pk,
            "owner": self.user.pk,
        }, htmx=True), deal.pk)
        self.assertEqual(post_response.status_code, 200)
        deal.refresh_from_db()
        self.assertEqual(deal.title, "Negócio aberto")
        self.assertEqual(deal.pipeline_id, self.pipeline.pk)


class PipelineStageTests(TenantTestBase):
    def test_pipeline_customizer_renders_stage_navigation_and_editor(self):
        pipeline = Pipeline.objects.create(workspace=self.tenant, name="Comercial")
        stage = pipeline.stages.create(key="entrada", name="Entrada", order=0)
        request = RequestFactory().get("/crm/pipelines/1/customize/")
        request.workspace = self.tenant

        from django.template.loader import render_to_string

        html = render_to_string(
            "crm/partials/pipeline_customize.html",
            _pipeline_modal_context(request, pipeline),
            request=request,
        )

        self.assertIn("pipeline-workspace__nav", html)
        self.assertIn("pipeline-stage-editor", html)
        self.assertIn(f"activeStage: '{stage.pk}'", html)
        self.assertNotIn("stage-row__toggle", html)

    def test_ensure_stages_creates_the_default_set(self):
        pipeline = Pipeline.objects.create(workspace=self.tenant, name="Vendas")
        pipeline.ensure_stages()

        self.assertEqual(pipeline.stages.count(), 6)
        self.assertEqual(
            list(pipeline.stages.values_list("key", flat=True)),
            ["novo", "qualificado", "proposta", "negociacao", "ganho", "perdido"],
        )

    def test_ensure_stages_is_idempotent(self):
        pipeline = Pipeline.objects.create(workspace=self.tenant, name="Vendas")
        pipeline.ensure_stages()
        pipeline.ensure_stages()

        self.assertEqual(pipeline.stages.count(), 6)

    def test_stage_key_is_unique_within_a_pipeline(self):
        pipeline = Pipeline.objects.create(workspace=self.tenant, name="Vendas")
        pipeline.stages.create(key="novo", name="Novo", order=0)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                pipeline.stages.create(key="novo", name="Duplicado", order=1)


class StageKindSyncTests(TenantTestBase):
    def test_sync_falls_back_to_default_stages_without_a_pipeline(self):
        for stage, expected in [("novo", "open"), ("ganho", "won"), ("perdido", "lost")]:
            deal = Deal(workspace=self.tenant, title="X", stage=stage)
            deal.sync_stage_kind()
            self.assertEqual(deal.stage_kind, expected, stage)

    def test_sync_reads_kind_from_the_pipeline_stage(self):
        pipeline = Pipeline.objects.create(workspace=self.tenant, name="P")
        pipeline.ensure_stages()
        deal = Deal.objects.create(
            workspace=self.tenant, title="X", pipeline=pipeline, stage="ganho"
        )
        deal.sync_stage_kind()
        self.assertEqual(deal.stage_kind, "won")

    def test_custom_stage_kind_overrides_the_default_mapping(self):
        # A stage whose key collides with a default ("novo") but is marked won.
        pipeline = Pipeline.objects.create(workspace=self.tenant, name="P")
        pipeline.stages.create(key="novo", name="Novo", color="#000000", kind="won", order=0)
        deal = Deal.objects.create(
            workspace=self.tenant, title="X", pipeline=pipeline, stage="novo"
        )
        deal.sync_stage_kind()
        self.assertEqual(deal.stage_kind, "won")


class StageDisplayTests(TenantTestBase):
    def test_display_and_color_fall_back_to_defaults(self):
        deal = Deal(workspace=self.tenant, title="X", stage="proposta")
        self.assertEqual(deal.stage_display, "Proposta")
        self.assertEqual(deal.stage_color, "#8b5cf6")

    def test_display_and_color_come_from_the_pipeline_stage(self):
        pipeline = Pipeline.objects.create(workspace=self.tenant, name="P")
        pipeline.stages.create(
            key="novo", name="Entrada", color="#111111", kind="open", order=0
        )
        deal = Deal.objects.create(
            workspace=self.tenant, title="X", pipeline=pipeline, stage="novo"
        )
        self.assertEqual(deal.stage_display, "Entrada")
        self.assertEqual(deal.stage_color, "#111111")


class TagScopeTests(TenantTestBase):
    def test_tag_name_is_unique_per_workspace(self):
        Tag.objects.create(workspace=self.tenant, name="VIP")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Tag.objects.create(workspace=self.tenant, name="VIP")


class TenantManagerScopeTests(TenantTestBase):
    """The default manager is a belt-and-suspenders filter on top of the schema
    boundary: when a current workspace is set, it scopes queries to it."""

    def tearDown(self):
        clear_current_workspace()
        super().tearDown()

    def test_default_manager_scopes_to_the_current_workspace(self):
        Company.objects.create(workspace=self.tenant, name="Acme")

        set_current_workspace(self.tenant)
        self.assertEqual(Company.objects.count(), 1)

        # A different workspace id must not see this workspace's rows.
        from core.models import Workspace

        other = Workspace(pk=self.tenant.pk + 100000, name="Outro", schema_name="outro")
        set_current_workspace(other)
        self.assertEqual(Company.objects.count(), 0)

        # With no workspace bound (migrations / shell), nothing is filtered out.
        clear_current_workspace()
        self.assertEqual(Company.all_objects.count(), 1)


class ConditionEngineTests(TenantTestBase):
    """The rich automation condition engine: field + operator + value + AND/OR."""

    def _event(self, obj, **payload):
        data = {"object_model": obj.__class__.__name__.lower(), "object_id": obj.pk, **payload}
        return Event(workspace=self.tenant, event_type="deal_stage_changed", payload=data)

    def _cond(self, deal, rules, match="all"):
        return _condition_matches(None, self._event(deal), deal, {"condition": {"match": match, "rules": rules}})

    def test_numeric_operators(self):
        deal = Deal.objects.create(workspace=self.tenant, title="Big", value=8000, stage="proposta")
        self.assertTrue(self._cond(deal, [{"field": "value", "op": "gt", "value": "5000"}]))
        self.assertFalse(self._cond(deal, [{"field": "value", "op": "lt", "value": "5000"}]))
        self.assertTrue(self._cond(deal, [{"field": "value", "op": "gte", "value": "8000"}]))

    def test_and_requires_all_or_requires_any(self):
        deal = Deal.objects.create(workspace=self.tenant, title="X", value=1000, stage="ganho")
        rules = [{"field": "value", "op": "gt", "value": "5000"}, {"field": "stage", "op": "eq", "value": "ganho"}]
        self.assertFalse(self._cond(deal, rules, match="all"))  # value fails
        self.assertTrue(self._cond(deal, rules, match="any"))   # stage passes

    def test_tag_membership(self):
        deal = Deal.objects.create(workspace=self.tenant, title="X", value=0, stage="novo")
        deal.tags.add(Tag.objects.create(workspace=self.tenant, name="Quente"))
        self.assertTrue(self._cond(deal, [{"field": "tag", "op": "contains", "value": "Quente"}]))
        self.assertFalse(self._cond(deal, [{"field": "tag", "op": "contains", "value": "Frio"}]))

    def test_empty_and_not_empty(self):
        deal = Deal.objects.create(workspace=self.tenant, title="No tags", value=0, stage="novo")
        self.assertTrue(self._cond(deal, [{"field": "tag", "op": "is_empty"}]))
        deal.tags.add(Tag.objects.create(workspace=self.tenant, name="VIP"))
        self.assertTrue(self._cond(deal, [{"field": "tag", "op": "is_not_empty"}]))

    def test_changed_to_uses_event_payload(self):
        deal = Deal.objects.create(workspace=self.tenant, title="X", value=0, stage="ganho")
        matched = _condition_matches(
            None, self._event(deal, stage="ganho"), deal,
            {"condition": {"match": "all", "rules": [{"field": "stage", "op": "changed_to", "value": "ganho"}]}},
        )
        self.assertTrue(matched)
        missed = _condition_matches(
            None, self._event(deal, stage="ganho"), deal,
            {"condition": {"match": "all", "rules": [{"field": "stage", "op": "changed_to", "value": "perdido"}]}},
        )
        self.assertFalse(missed)

    def test_custom_field_condition(self):
        CustomField.objects.create(
            workspace=self.tenant, object_type="deal", key="prioridade", label="Prioridade", field_type="text"
        )
        deal = Deal.objects.create(
            workspace=self.tenant, title="X", value=0, stage="novo", custom={"prioridade": "alta"}
        )
        self.assertTrue(self._cond(deal, [{"field": "custom:deal:prioridade", "op": "eq", "value": "alta"}]))
        self.assertFalse(self._cond(deal, [{"field": "custom:deal:prioridade", "op": "eq", "value": "baixa"}]))

    def test_legacy_single_field_condition_still_works(self):
        deal = Deal.objects.create(workspace=self.tenant, title="X", value=0, stage="ganho")
        ev = self._event(deal)
        self.assertTrue(_condition_matches(None, ev, deal, {"condition_stage": "ganho"}))
        self.assertFalse(_condition_matches(None, ev, deal, {"condition_stage": "perdido"}))
