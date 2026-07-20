"""Run automations that start from a schedule trigger.

    python manage.py process_scheduled_automations
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, get_tenant_model, tenant_context

from core.events import automation_trigger_data, run_automation_for_event
from core.models import Automation, Event
from core.tenancy import clear_current_workspace, set_current_workspace


class Command(BaseCommand):
    help = "Run active schedule trigger automations when their interval is due."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        Workspace = get_tenant_model()
        limit = max(1, int(options["limit"]))
        now = timezone.now()
        total = 0

        for ws in Workspace.objects.exclude(schema_name=get_public_schema_name()):
            with tenant_context(ws):
                set_current_workspace(ws)
                automations = Automation.objects.filter(
                    workspace=ws,
                    active=True,
                    trigger="schedule_interval",
                )[:limit]
                for auto in automations:
                    data = automation_trigger_data(auto)
                    minutes = _interval_minutes(data)
                    last_run = auto.runs.exclude(status="test").order_by("-created_at").first()
                    if last_run and last_run.created_at > now - timedelta(minutes=minutes):
                        continue
                    event = Event.objects.create(
                        workspace=ws,
                        event_type="schedule_interval",
                        object_repr=f"A cada {minutes} minuto(s)",
                        payload={"trigger_interval_minutes": minutes},
                    )
                    run_automation_for_event(auto, event, None)
                    event.processed = True
                    event.save(update_fields=["processed"])
                    total += 1
                clear_current_workspace()

        self.stdout.write(self.style.SUCCESS(f"Executadas {total} automacao(oes) agendada(s)."))


def _interval_minutes(data):
    try:
        amount = int(data.get("trigger_interval_amount") or data.get("trigger_interval_minutes") or 1)
    except (TypeError, ValueError):
        amount = 1
    unit = data.get("trigger_interval_unit") or "hours"
    multiplier = {"minutes": 1, "hours": 60, "days": 1440}.get(unit, 60)
    return max(1, min(amount * multiplier, 10080))
