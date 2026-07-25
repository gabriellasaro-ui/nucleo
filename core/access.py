"""Platform-level access: account types (admin / agency / user) and which
workspaces a user may enter.

Single source of truth reused by the middleware, workspace switching and the
UI. Keeps the in-workspace Membership role untouched — this is a separate axis
sitting *above* workspaces (the reseller/SaaS layer).
"""
from functools import wraps

from django.http import HttpResponseForbidden
from django_tenants.utils import get_public_schema_name

from .models import Membership, UserProfile, Workspace


def get_profile(user):
    """The user's platform profile, created lazily on first access."""
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def account_type(user):
    if not user or not user.is_authenticated:
        return UserProfile.TYPE_USER
    if user.is_superuser:            # bootstrap: the owner is a superuser -> admin
        return UserProfile.TYPE_ADMIN
    return get_profile(user).account_type


def is_platform_admin(user):
    return bool(user and user.is_authenticated
                and (user.is_superuser or account_type(user) == UserProfile.TYPE_ADMIN))


def is_agency(user):
    return account_type(user) == UserProfile.TYPE_AGENCY


class SyntheticMembership:
    """Stand-in Membership for an admin/agency entering a workspace they don't
    belong to. Grants owner-level power so every existing RBAC check keeps
    working unchanged (`require_role`, `can_edit` read `request.membership`)."""
    role = Membership.ROLE_OWNER

    def __init__(self, workspace=None, label="Gestor"):
        self.workspace = workspace
        self.workspace_id = getattr(workspace, "pk", None)
        self._label = label

    @property
    def level(self):
        return Membership.ROLE_LEVEL[Membership.ROLE_OWNER]

    def can(self, min_role):
        return self.level >= Membership.ROLE_LEVEL.get(min_role, 99)

    def get_role_display(self):
        return self._label


def platform_admin_required(view):
    """View decorator: only platform admins may proceed."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_platform_admin(getattr(request, "user", None)):
            return HttpResponseForbidden("Restrito à administração da plataforma.")
        return view(request, *args, **kwargs)
    return wrapped


def agency_required(view):
    """View decorator: only agency (or platform admin) accounts may proceed."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not (is_agency(user) or is_platform_admin(user)):
            return HttpResponseForbidden("Restrito a agências.")
        return view(request, *args, **kwargs)
    return wrapped


def can_access_workspace(user, ws):
    if not user or not user.is_authenticated or ws is None:
        return False
    if is_platform_admin(user):
        return True
    if Membership.objects.filter(user=user, workspace=ws).exists():
        return True
    if is_agency(user) and ws.agency_id == user.id:
        return True
    return False


def accessible_workspaces(user):
    """Ordered list of {workspace, relation, role} the user may enter.
    relation: 'own' (member), 'agency' (manages as agency), 'admin' (platform).
    Own workspaces come first, then the rest by name."""
    if not user or not user.is_authenticated:
        return []
    memberships = {
        m.workspace_id: m
        for m in Membership.objects.filter(user=user).select_related("workspace")
    }
    rows, seen = [], set()

    if is_platform_admin(user):
        for ws in Workspace.objects.exclude(schema_name=get_public_schema_name()).order_by("name"):
            m = memberships.get(ws.pk)
            rows.append({"workspace": ws, "relation": "own" if m else "admin",
                         "role": m.get_role_display() if m else "Admin"})
            seen.add(ws.pk)
        return rows

    for ws_id, m in memberships.items():
        rows.append({"workspace": m.workspace, "relation": "own", "role": m.get_role_display()})
        seen.add(ws_id)

    if is_agency(user):
        for ws in Workspace.objects.filter(agency=user).exclude(pk__in=seen).order_by("name"):
            rows.append({"workspace": ws, "relation": "agency", "role": "Agência"})
            seen.add(ws.pk)

    rows.sort(key=lambda r: (r["relation"] != "own", (r["workspace"].name or "").lower()))
    return rows
