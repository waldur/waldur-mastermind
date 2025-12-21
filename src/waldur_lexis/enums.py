from django.db import models
from django.utils.translation import gettext_lazy as _


class LexisLinkStates(models.IntegerChoices):
    PENDING = 1, _("pending")
    EXECUTING = 2, _("executing")
    OK = 3, _("OK")
    ERRED = 4, _("erred")
