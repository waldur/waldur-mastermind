from django.db import models
from django.utils.translation import gettext_lazy as _


class BroadcastMessageState(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    SCHEDULED = "SCHEDULED", _("Scheduled")
    SENT = "SENT", _("Sent")


class AdminAnnouncementType(models.TextChoices):
    INFORMATION = "information", _("Information")
    WARNING = "warning", _("Warning")
    DANGER = "danger", _("Danger")
