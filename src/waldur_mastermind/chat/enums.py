from django.db import models
from django.utils.translation import gettext_lazy as _


class FeedbackCategory(models.TextChoices):
    """Valid categories for negative assistant-message feedback."""

    INACCURATE = "inaccurate", _("Inaccurate")
    INCOMPLETE = "incomplete", _("Incomplete")
    MISUNDERSTOOD = "misunderstood", _("Misunderstood")
    SLOW_OR_FAILED = "slow_or_failed", _("Slow or failed")
    OTHER = "other", _("Other")
