from django.db import models
from django.utils.translation import gettext_lazy as _


class AuthResultStates(models.TextChoices):
    SCHEDULED = "Scheduled", _("Scheduled")
    PROCESSING = "Processing", _("Processing")
    OK = "OK", _("OK")
    CANCELED = "Canceled", _("Canceled")
    ERRED = "Erred", _("Erred")
