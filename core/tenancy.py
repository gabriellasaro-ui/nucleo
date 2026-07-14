"""Automatic tenant isolation.

A thread-local holds the *current workspace* for the duration of a request
(set by WorkspaceMiddleware). Models that use `TenantManager` as their default
manager are then filtered by that workspace automatically — so a forgotten
`.filter(workspace=...)` can no longer leak data across tenants.

Outside a request (migrations, management commands, shell) no workspace is set,
so the manager returns everything — exactly what those contexts need. An
unscoped `all_objects` manager is always available as an explicit escape hatch.
"""
import threading

from django.db import models

_state = threading.local()


def set_current_workspace(workspace):
    _state.workspace = workspace


def get_current_workspace():
    return getattr(_state, "workspace", None)


def clear_current_workspace():
    _state.workspace = None


class TenantManager(models.Manager):
    """Default manager that scopes every query to the current workspace."""

    def get_queryset(self):
        qs = super().get_queryset()
        workspace = get_current_workspace()
        if workspace is not None:
            return qs.filter(workspace=workspace)
        return qs
