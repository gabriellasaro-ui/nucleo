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

from .models import Membership
from .tenancy import clear_current_workspace, set_current_workspace


class WorkspaceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.memberships = []
        request.membership = None
        request.workspace = None

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            memberships = list(
                Membership.objects.filter(user=user).select_related("workspace")
            )
            request.memberships = memberships
            if memberships:
                ws_id = request.session.get("workspace_id")
                chosen = next((m for m in memberships if m.workspace_id == ws_id), None)
                if chosen is None:
                    chosen = memberships[0]
                    request.session["workspace_id"] = chosen.workspace_id
                request.membership = chosen
                request.workspace = chosen.workspace
            elif self._needs_workspace(request):
                return redirect("workspace_new")

        set_current_workspace(request.workspace)
        if request.workspace is not None:
            # Route every query in this request to the workspace's own schema.
            connection.set_tenant(request.workspace)
        try:
            return self.get_response(request)
        finally:
            clear_current_workspace()
            connection.set_schema_to_public()

    @staticmethod
    def _needs_workspace(request):
        path = request.path
        if path.startswith(("/admin", "/accounts", "/static", "/media")):
            return False
        if path == reverse("workspace_new"):
            return False
        return True
