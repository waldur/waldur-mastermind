from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from model_utils.fields import AutoCreatedField

from waldur_core.core.models import DescribableMixin, NameMixin, TimeStampedModel
from waldur_core.core.validators import validate_name
from waldur_core.logging.models import UuidMixin

User = get_user_model()


class MessageTemplate(UuidMixin, NameMixin):
    body = models.TextField(blank=False)
    subject = models.TextField(blank=False)


class BroadcastMessage(UuidMixin):
    class States:
        DRAFT = "DRAFT"
        SCHEDULED = "SCHEDULED"
        SENT = "SENT"

        CHOICES = (
            (DRAFT, _("Draft")),
            (SCHEDULED, _("Scheduled")),
            (SENT, _("Sent")),
        )

    state = models.CharField(
        max_length=30, choices=States.CHOICES, default=States.DRAFT
    )
    send_at = models.DateField(null=True)
    author = models.ForeignKey(to=User, on_delete=models.SET_NULL, null=True)
    created = AutoCreatedField()
    subject = models.CharField(max_length=1000, validators=[validate_name])
    body = models.TextField(validators=[validate_name])
    query = models.JSONField()
    emails = models.JSONField()


class AdminAnnouncement(UuidMixin, DescribableMixin, TimeStampedModel):
    class Type:
        INFORMATION = "information"
        WARNING = "warning"
        DANGER = "danger"

        CHOICES = (
            (INFORMATION, _("Information")),
            (WARNING, _("Warning")),
            (DANGER, _("Danger")),
        )

    type = models.CharField(
        max_length=30, choices=Type.CHOICES, default=Type.INFORMATION
    )
    active_from = models.DateTimeField()
    active_to = models.DateTimeField()

    @property
    def is_active(self) -> bool:
        return self.active_from <= timezone.now() <= self.active_to
