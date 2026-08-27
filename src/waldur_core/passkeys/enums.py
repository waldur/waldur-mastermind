from django.db import models
from django.utils.translation import gettext_lazy as _


class CeremonyKind(models.TextChoices):
    """What a ceremony is for.

    The kind fixes which finish endpoint may consume the row, so a challenge
    issued for enrolling a new credential can never be replayed against a
    sign-in, and vice versa.
    """

    REGISTRATION = "registration", _("Registration")
    SIGNIN = "signin", _("Passwordless sign-in")
    MFA = "mfa", _("Second factor")


class AuthenticatorAttachment(models.TextChoices):
    PLATFORM = "platform", _("Platform")
    CROSS_PLATFORM = "cross-platform", _("Cross-platform")
    UNKNOWN = "", _("Unknown")
