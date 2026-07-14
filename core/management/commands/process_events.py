"""Drain unprocessed outbox events (the async path of the outbox pattern).

    python manage.py process_events
"""
from django.core.management.base import BaseCommand

from core.events import process
from core.models import Event


class Command(BaseCommand):
    help = "Process any unprocessed outbox events."

    def handle(self, *args, **options):
        pending = Event.objects.filter(processed=False)
        count = 0
        for event in pending:
            process(event)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Processados {count} evento(s)."))
