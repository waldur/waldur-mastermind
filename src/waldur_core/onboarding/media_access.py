"""Media access rules for files owned by the onboarding app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.core.models import User
from waldur_core.media import access
from waldur_core.media import models as media_models
from waldur_core.onboarding.models import OnboardingJustificationDocumentation


def user_can_access_justification_documentation(
    file: media_models.File, user: User
) -> bool:
    """Mirror StaffOrUserFilter, which OnboardingJustificationViewSet applies."""
    if not user.is_authenticated:
        return False
    queryset = OnboardingJustificationDocumentation.objects.filter(file=file.name)
    if not (user.is_staff or user.is_support):
        queryset = queryset.filter(justification__user=user)
    return queryset.exists()


access.register(
    access.upload_prefix(OnboardingJustificationDocumentation, "file"),
    user_can_access_justification_documentation,
)
