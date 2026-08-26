from typing import Any

from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _

from waldur_core.core.user_attributes import get_user_missing_mandatory_attributes
from waldur_core.user_actions.providers import (
    BaseDashboardProvider,
    register_dashboard_provider,
)

User = get_user_model()


class ProfileCompletenessProvider(BaseDashboardProvider):
    """Provider for users with missing mandatory profile attributes."""

    action_type = "profile_incomplete"
    display_name = "Profile Completeness"

    def get_dashboard_pending_actions(self, user: User) -> list[dict[str, Any]]:
        missing = get_user_missing_mandatory_attributes(user)
        if not missing:
            return []
        return [
            {
                "type": "profile_incomplete",
                "title": _("Complete your profile"),
                "description": _("The following fields are required: %(fields)s.")
                % {"fields": ", ".join(missing)},
                "variant": "info",
                "deadline": None,
                "count": None,
                "target_uuid": None,
                "customer_uuid": None,
            }
        ]


register_dashboard_provider(ProfileCompletenessProvider)
