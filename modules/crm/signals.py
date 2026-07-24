"""Wire the audit trail (LGPD Fase 2) to model and auth events.

CRUD on personal-data models is captured automatically, so no view needs to
remember to log it. Detail-page *views* and *exports* are logged explicitly
(they aren't model events). Connected once from CrmConfig.ready().
"""
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_delete, post_save

from core import audit

from .models import Company, Contact, Deal

# The personal-data models whose lifecycle we track.
_AUDITED = (Contact, Company, Deal)


def _log(instance, action):
    audit.record(
        action,
        object_type=instance._meta.model_name,   # "contact" / "company" / "deal"
        object_id=instance.pk,
        object_repr=str(instance),
    )


def _on_save(sender, instance, created, **kwargs):
    _log(instance, "create" if created else "update")


def _on_delete(sender, instance, **kwargs):
    _log(instance, "delete")


def _on_login(sender, request, user, **kwargs):
    audit.record_auth(user, "login", request)


def _on_logout(sender, request, user, **kwargs):
    audit.record_auth(user, "logout", request)


def connect():
    """Idempotent (dispatch_uid) so autoreload/re-imports don't double-fire."""
    for model in _AUDITED:
        post_save.connect(_on_save, sender=model, dispatch_uid=f"audit_save_{model._meta.label}")
        post_delete.connect(_on_delete, sender=model, dispatch_uid=f"audit_delete_{model._meta.label}")
    user_logged_in.connect(_on_login, dispatch_uid="audit_login")
    user_logged_out.connect(_on_logout, dispatch_uid="audit_logout")
