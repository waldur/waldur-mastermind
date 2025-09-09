from django.db import models
from django.utils.translation import gettext_lazy as _


class ProjectKind(models.TextChoices):
    DEFAULT = "default", _("Default")
    COURSE = "course", _("Course")
    PUBLIC = "public", _("Public")
