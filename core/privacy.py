"""LGPD privacy primitives (Fases 3–4): legal basis / consent vocabulary,
contact anonymisation, and the retention query.

Anonymisation keeps the record (so deal history / metrics stay intact) but wipes
the personal data — this is what both automatic retention (Fase 3) and the
on-demand right-to-erasure (Fase 4) call.
"""
from datetime import timedelta

from django.utils import timezone

# Consent state of a contact.
CONSENT_UNKNOWN = "unknown"
CONSENT_GRANTED = "granted"
CONSENT_WITHDRAWN = "withdrawn"
CONSENT_STATUS_CHOICES = [
    (CONSENT_UNKNOWN, "Não informado"),
    (CONSENT_GRANTED, "Consentido"),
    (CONSENT_WITHDRAWN, "Revogado"),
]

# Legal basis for processing (LGPD art. 7).
BASIS_CHOICES = [
    ("", "Não definida"),
    ("consentimento", "Consentimento"),
    ("legitimo_interesse", "Legítimo interesse"),
    ("contrato", "Execução de contrato"),
    ("obrigacao_legal", "Obrigação legal"),
]

_ANON = "[removido]"


def anonymize_contact(contact, *, reason="erasure", source="user"):
    """Wipe personal data from a contact in place, keeping the row. Idempotent.
    Records an audit event. Returns True if it changed anything."""
    from core import audit

    if getattr(contact, "is_anonymized", False):
        return False
    label = str(contact)
    contact.first_name = _ANON
    contact.last_name = ""
    contact.email = ""
    contact.phone = ""
    contact.job_title = ""
    contact.address_street = ""
    contact.address_number = ""
    contact.address_complement = ""
    contact.district = ""
    contact.city = ""
    contact.state = ""
    contact.zipcode = ""
    contact.custom = {}
    contact.consent_status = CONSENT_WITHDRAWN
    contact.is_anonymized = True
    contact.anonymized_at = timezone.now()
    contact.save()
    audit.record(
        "anonymize", object_type="contact", object_id=contact.pk,
        object_repr=label, changes={"reason": reason}, source=source,
    )
    return True


def retention_cutoff(months, now=None):
    """The moment before which untouched data is considered expired."""
    now = now or timezone.now()
    return now - timedelta(days=30 * months)


def expired_contacts(ws, now=None):
    """Contacts eligible for retention purge: untouched past the workspace's
    window, not already anonymised, and NOT tied to an open/won deal (never
    nuke an active relationship)."""
    from modules.crm.models import Contact, Deal

    months = getattr(ws, "retention_months", 0) or 0
    if months <= 0:
        return Contact.all_objects.none()
    cutoff = retention_cutoff(months, now)
    active_ids = (
        Deal.all_objects.filter(workspace=ws, stage_kind__in=["open", "won"], contact__isnull=False)
        .values_list("contact_id", flat=True)
    )
    return (
        Contact.all_objects.filter(workspace=ws, is_anonymized=False, updated_at__lt=cutoff)
        .exclude(pk__in=active_ids)
    )
