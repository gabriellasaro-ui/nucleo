"""Role-based access control helpers. Single decision point: a request's
active Membership decides what it may do, scoped to the active workspace.
"""
from functools import wraps

from django.http import HttpResponseForbidden


def require_role(min_role):
    """View decorator: the active membership must be at least `min_role`."""
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            membership = getattr(request, "membership", None)
            if membership is None or not membership.can(min_role):
                return HttpResponseForbidden("Você não tem permissão para esta ação.")
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def can_edit(request):
    """Members and above can create/edit/delete records; viewers are read-only."""
    membership = getattr(request, "membership", None)
    return bool(membership and membership.can("member"))
