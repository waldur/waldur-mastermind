"""Who may use the AI assistant, by AI_ASSISTANT_ENABLED_ROLES.

One check for every caller: the chat API, and features of other apps that ask
the assistant's model on a user's behalf (the Matrix bot's ``!ticket``).
"""

from django.utils.translation import gettext_lazy as _

from waldur_core.configuration import config


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
