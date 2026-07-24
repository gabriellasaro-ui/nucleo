"""Audit trail (LGPD Fase 2).

Records who did what to personal data, per workspace. The AuditLog model lives
in the tenant schema (modules.crm), so each workspace's trail is isolated with
its own data — same isolation promise as the rest of the CRM.

Design:
- A thread-local holds the current actor (user) + client IP, set by
  WorkspaceMiddleware for the duration of a request.
- `record()` is the hot path: called during a request that already has the
  tenant schema active (CRUD signals, detail-view access). It never raises —
  auditing must never compromise the state of the CRM.
- `record_auth()` handles login/logout, which happen with no tenant active, by
  writing into the user's workspace schema via tenant_context.
"""
import logging
import threading

log = logging.getLogger("nucleo.audit")

_state = threading.local()


def set_actor(user, ip=None):
    _state.user = user
    _state.ip = ip


def get_actor():
    return getattr(_state, "user", None), getattr(_state, "ip", None)


def clear_actor():
    _state.user = None
    _state.ip = None


def client_ip(request):
    """First hop of X-Forwarded-For (we're behind Easypanel/Traefik), else peer."""
    if request is None:
        return None
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def record(action, *, object_type="", object_id="", object_repr="",
           changes=None, workspace=None, actor=None, ip=None, source=None):
    """Write one audit row into the *current* tenant schema. Best-effort."""
    try:
        from modules.crm.models import AuditLog
        from core.tenancy import get_current_workspace

        ws = workspace or get_current_workspace()
        if ws is None:
            return  # no workspace context -> nothing to attach the event to

        a_user, a_ip = get_actor()
        actor = actor if actor is not None else a_user
        ip = ip if ip is not None else a_ip
        actor_obj = actor if (actor is not None and getattr(actor, "is_authenticated", False)) else None
        actor_label = (actor_obj.get_username() or "")[:180] if actor_obj is not None else ""
        if source is None:
            source = "user" if actor_obj is not None else "system"

        AuditLog.all_objects.create(
            workspace=ws,
            actor=actor_obj,
            actor_label=actor_label,
            action=action,
            object_type=object_type or "",
            object_id=str(object_id or "")[:40],
            object_repr=(object_repr or "")[:200],
            changes=changes or {},
            ip=ip,
            source=source,
        )
    except Exception:
        log.exception("audit.record failed (action=%s)", action)


def record_auth(user, action, request=None):
    """Login/logout happen outside any tenant context: attach the event to the
    user's workspace schema. Best-effort; silent if the user has none."""
    try:
        from django_tenants.utils import tenant_context
        from core.models import Membership

        if user is None or not getattr(user, "is_authenticated", False):
            return
        membership = (
            Membership.objects.filter(user=user).select_related("workspace").first()
        )
        if membership is None:
            return
        ws = membership.workspace
        with tenant_context(ws):
            record(action, workspace=ws, actor=user, ip=client_ip(request), source="user")
    except Exception:
        log.exception("audit.record_auth failed (action=%s)", action)
