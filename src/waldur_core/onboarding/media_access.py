"""Media access rules for files owned by the onboarding app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.core.models import User
from waldur_core.core.permissions import has_support_access
from waldur_core.media import access
from waldur_core.media import models as media_models
from waldur_core.onboarding.models import OnboardingJustificationDocumentation


def user_can_access_justification_documentation(
    file: media_models.File, principal
) -> bool:
    """Mirror StaffOrUserFilter, which OnboardingJustificationViewSet applies.

    ``principal`` is a request or a user. A request acts as support only if a
    personal access token behind it carries the staff or support scope.
    """
    if isinstance(principal, User):
        user = principal
        acts_as_support = user.is_staff or user.is_support
    else:
        user = principal.user
        acts_as_support = has_support_access(principal)
    if not user.is_authenticated:
        return False
    queryset = OnboardingJustificationDocumentation.objects.filter(file=file.name)
    if not acts_as_support:
        queryset = queryset.filter(justification__user=user)
    return queryset.exists()


access.register(
    access.upload_prefix(OnboardingJustificationDocumentation, "file"),
    user_can_access_justification_documentation,
    with_request=True,
)
