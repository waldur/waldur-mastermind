"""Who may use the AI assistant, by AI_ASSISTANT_ENABLED_ROLES.

One check for every caller: the chat API, and features of other apps that ask
the assistant's model on a user's behalf (the Matrix bot's ``!ticket``).
"""

from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from waldur_core.configuration import config
from waldur_core.core import access as core_access


def role_refusal(user):
    """Why AI_ASSISTANT_ENABLED_ROLES keeps ``user`` from the assistant, or
    None if it lets them in."""
    enabled_roles = config.AI_ASSISTANT_ENABLED_ROLES
    if enabled_roles == "disabled":
        return _("AI Assistant is not available.")
    if enabled_roles == "staff":
        if user.is_staff:
            return None
        return _("AI Assistant is currently available to staff users only.")
    if enabled_roles == "staff_and_support":
        if user.is_staff or user.is_support:
            return None
        return _("AI Assistant is currently available to staff and support users only.")
    if enabled_roles not in ("all", "anonymous"):
        return _("AI Assistant access is not configured correctly.")
    return None


class AssistantRoleEnabled(BasePermission):
    """Let through only the users AI_ASSISTANT_ENABLED_ROLES admits.

    The assistant's views list it after ``IsAuthenticated``, so it sees a
    signed-in user; it answers with ``role_refusal``'s reason.
    """

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        return [
            core_access.custom(
                "AI_ASSISTANT_ENABLED_ROLES admits the caller: every user, staff "
                "and support only, staff only, or no one, as configured."
            )
        ]

    def has_permission(self, request, view):
        refusal = role_refusal(request.user)
        if refusal:
            raise PermissionDenied(refusal)
        return True
