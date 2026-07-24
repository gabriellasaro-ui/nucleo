"""LGPD retention: anonymize contacts untouched past each workspace's window.

    python manage.py anonymize_expired            # apply
    python manage.py anonymize_expired --dry-run  # just report

Run it on a schedule (cron / Easypanel scheduled task). Idempotent and safe:
contacts tied to an open/won deal are never touched.
"""
from django.core.management.base import BaseCommand
from django_tenants.utils import get_public_schema_name, get_tenant_model, tenant_context

from core.privacy import anonymize_contact, expired_contacts
from core.tenancy import clear_current_workspace, set_current_workspace


class Command(BaseCommand):
    help = "Anonimiza contatos sem atividade além da janela de retenção do workspace."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Só reporta, não altera.")

    def handle(self, *args, **options):
        Workspace = get_tenant_model()
        dry = options["dry_run"]
        total = 0

        for ws in Workspace.objects.exclude(schema_name=get_public_schema_name()):
            if (getattr(ws, "retention_months", 0) or 0) <= 0:
                continue
            with tenant_context(ws):
                set_current_workspace(ws)
                try:
                    victims = list(expired_contacts(ws))
                    if dry:
                        self.stdout.write(f"[{ws.name}] {len(victims)} contato(s) expirado(s) (dry-run).")
                    else:
                        done = sum(1 for c in victims if anonymize_contact(c, reason="retention", source="system"))
                        total += done
                        self.stdout.write(f"[{ws.name}] {done} contato(s) anonimizado(s).")
                finally:
                    clear_current_workspace()

        verb = "seriam anonimizados" if dry else "anonimizados"
        self.stdout.write(self.style.SUCCESS(f"Concluído: {total} contato(s) {verb}."))
