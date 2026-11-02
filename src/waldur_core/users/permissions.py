"""Who may act on an invitation or a permission request.

Each check enforces one rule and describes it in the API schema, so what a
route enforces and what the schema says come from one place.
"""

from django.utils.translation import gettext as _
from rest_framework import exceptions

from waldur_core.core import access
from waldur_core.core.permissions import has_staff_access
from waldur_core.users.utils import (
    MANAGES_INVITATION_SCOPE,
    MANAGES_PERMISSION_REQUEST,
    can_manage_invitation_with,
    can_manage_permission_request,
)


@access.describes(MANAGES_INVITATION_SCOPE, needs_object=True)
def can_manage_invitation(request, view, obj=None):
    if obj is not None and not can_manage_invitation_with(request, obj.scope):
        # Not found rather than denied, so that a caller who may not manage
        # the invitation is not told it exists.
        raise exceptions.NotFound()


@access.describes(MANAGES_PERMISSION_REQUEST, needs_object=True)
def can_review_permission_request(request, view, obj=None):
    if obj is not None and not can_manage_permission_request(request, obj.invitation):
        # Not found rather than denied, as for invitations.
        raise exceptions.NotFound()


@access.describes(
    access.any_of(access.owned("The request's author."), access.staff()),
    needs_object=True,
)
def is_permission_request_author(request, view, obj=None):
    if (
        obj is not None
        and obj.created_by != request.user
        and not has_staff_access(request)
    ):
        raise exceptions.PermissionDenied(
            _("You can only cancel your own permission requests.")
        )
