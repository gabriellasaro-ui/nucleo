"""Resolves the active workspace for each request (multi-tenancy).

Sets on the request:
  request.memberships  -> list of the user's Membership objects
  request.membership   -> the active Membership (or None)
  request.workspace    -> the active Workspace (or None)

If the user is authenticated but has no workspace yet, they are redirected to
the onboarding page to create one.
"""
from django.db import connection
from django.shortcuts import redirect
from django.urls import reverse

from . import access, audit
from .models import Membership
from .tenancy import clear_current_workspace, set_current_workspace


class WorkspaceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.memberships = []
        request.accessible_workspaces = []
        request.membership = None
        request.workspace = None
        request.account_type = "user"
        request.is_workspace_manager = False

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            request.account_type = access.account_type(user)
            request.memberships = list(
                Membership.objects.filter(user=user).select_related("workspace")
            )
            # Accessible = own memberships + (agency) managed clients + (admin) all.
            rows = access.accessible_workspaces(user)
            request.accessible_workspaces = rows
            if rows:
                ws_id = request.session.get("workspace_id")
                chosen = next((r for r in rows if r["workspace"].pk == ws_id), None)
                if chosen is None:
                    chosen = rows[0]
                    request.session["workspace_id"] = chosen["workspace"].pk
                ws = chosen["workspace"]
                request.workspace = ws
                real = next((m for m in request.memberships if m.workspace_id == ws.pk), None)
                if real is not None:
                    request.membership = real
                else:
                    # Admin/agency entering a workspace they don't belong to:
                    # owner-level power so existing RBAC keeps working.
                    request.membership = access.SyntheticMembership(ws)
                    request.is_workspace_manager = True
            elif self._needs_workspace(request):
                return redirect("workspace_new")

        set_current_workspace(request.workspace)
        # Remember who is acting (+ their IP) so the audit trail can attribute
        # model changes without threading the request through every save.
        audit.set_actor(user if (user is not None and user.is_authenticated) else None,
                        audit.client_ip(request))
        if request.workspace is not None:
            # Route every query in this request to the workspace's own schema.
            connection.set_tenant(request.workspace)
        try:
            # A suspended workspace blocks its client, but a manager (agency/admin)
            # can still enter to manage or reactivate it. Inside the tenant context
            # so the page renders normally; the finally resets the schema.
            ws = request.workspace
            if ws is not None and ws.suspended and not request.is_workspace_manager \
                    and not access.is_platform_admin(user):
                if not request.path.startswith(("/accounts/logout", "/static", "/media")):
                    from django.shortcuts import render as _render
                    return _render(request, "core/suspended.html", {"workspace": ws}, status=403)
            return self.get_response(request)
        finally:
            clear_current_workspace()
            audit.clear_actor()
            connection.set_schema_to_public()

    @staticmethod
    def _needs_workspace(request):
        path = request.path
        if path.startswith(("/admin", "/accounts", "/static", "/media", "/console", "/agencia")):
            return False
        if path == reverse("workspace_new"):
            return False
        return True
