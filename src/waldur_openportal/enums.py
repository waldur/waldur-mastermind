from django.db import models
from django.utils.translation import gettext_lazy as _


class JobState(models.TextChoices):
    PENDING = "pending", _("Pending")
    RUNNING = "running", _("Running")
    COMPLETED = "completed", _("Completed")
    COMMUNICATED = "communicated", _("Communicated")
    CANCELLED = "cancelled", _("Cancelled")
