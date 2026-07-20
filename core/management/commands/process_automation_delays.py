"""Resume automation flows paused by delay nodes.

    python manage.py process_automation_delays
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, get_tenant_model, tenant_context

from core.events import resume_scheduled_run
from core.models import AutomationRun
from core.tenancy import clear_current_workspace, set_current_workspace


class Command(BaseCommand):
    help = "Resume scheduled automation runs whose delay time has elapsed."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        Workspace = get_tenant_model()
        limit = max(1, int(options["limit"]))
        total = 0
        now = timezone.now()

        for ws in Workspace.objects.exclude(schema_name=get_public_schema_name()):
            with tenant_context(ws):
                set_current_workspace(ws)
                runs = list(
                    AutomationRun.objects.filter(
                        status="scheduled",
                        scheduled_for__lte=now,
                    )
                    .select_related("automation", "event")
                    .order_by("scheduled_for")[:limit]
                )
                for run in runs:
                    resume_scheduled_run(run)
                    total += 1
                clear_current_workspace()

        self.stdout.write(self.style.SUCCESS(f"Retomadas {total} execucao(oes) agendada(s)."))
