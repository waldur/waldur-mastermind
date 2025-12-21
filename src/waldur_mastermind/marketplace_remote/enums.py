from django.db import models
from django.utils.translation import gettext_lazy as _


class RemoteSynchronisationState(models.TextChoices):
    SCHEDULED = "Scheduled", _("Scheduled")
    PROCESSING = "Processing", _("Processing")
    OK = "OK", _("OK")
    ERRED = "Erred", _("Erred")
