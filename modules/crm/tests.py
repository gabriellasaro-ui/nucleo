"""Model-level tests for the CRM module.

These run inside a single disposable tenant schema (`TenantTestCase` creates the
`test` schema and points the connection at it), so every query here already lives
in an isolated workspace schema. Cross-tenant isolation itself is proved
separately in ``core.tests.SchemaIsolationTests``.
"""
from django.db import IntegrityError, transaction
from django_tenants.test.cases import TenantTestCase

from core.events import _condition_matches
from core.models import CustomField, Event
from core.tenancy import clear_current_workspace, set_current_workspace

from .models import Company, Deal, Pipeline, Tag


class TenantTestBase(TenantTestCase):
    """Give the auto-created test workspace a name (the field is required)."""

    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = "Tenant de Teste"


class PipelineStageTests(TenantTestBase):
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
