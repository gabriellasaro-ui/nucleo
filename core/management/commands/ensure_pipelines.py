"""Ensure every workspace has a default pipeline with stages, and backfill
each deal's pipeline + denormalized stage_kind. Runs across all tenant schemas.

    python manage.py ensure_pipelines
"""
from django.core.management.base import BaseCommand
from django_tenants.utils import get_public_schema_name, get_tenant_model, tenant_context

from modules.crm.models import DEFAULT_STAGES, Deal, Pipeline


class Command(BaseCommand):
    help = "Create a default pipeline per workspace and backfill deals."

    def handle(self, *args, **options):
        Workspace = get_tenant_model()
        kinds = {key: kind for key, _, _, kind in DEFAULT_STAGES}
        touched = 0
        for ws in Workspace.objects.exclude(schema_name=get_public_schema_name()):
            with tenant_context(ws):
                pipe = Pipeline.objects.filter(is_default=True).first() or Pipeline.objects.first()
                if pipe is None:
                    pipe = Pipeline.objects.create(workspace=ws, name="Vendas", is_default=True, order=0)
                pipe.ensure_stages()
                for deal in Deal.all_objects.all():
                    changed = False
                    if not deal.pipeline_id:
                        deal.pipeline = pipe
                        changed = True
                    new_kind = kinds.get(deal.stage, "open")
                    if deal.stage_kind != new_kind:
                        deal.stage_kind = new_kind
                        changed = True
                    if changed:
                        deal.save(update_fields=["pipeline", "stage_kind"])
            touched += 1
        self.stdout.write(self.style.SUCCESS(f"Pipelines garantidas em {touched} workspace(s)."))
