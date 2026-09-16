"""What SRAM (SURF Research Access Management) provisioned into Waldur.

SRAM pushes Users and Groups (collaborations and their sub-groups) over SCIM.
These rows record exactly what SRAM owns, so the SRAM SCIM endpoints can list,
update and delete only SRAM-provisioned objects: SRAM's sweep deletes whatever a
service lists that SRAM does not recognise.
"""

from django.conf import settings
from django.db import models
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models


class SramUser(TimeStampedModel):
    """Links a Waldur user to the SRAM identity that provisioned it."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sram_user",
    )
    external_id = models.CharField(max_length=255, unique=True)
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Last SCIM User resource received from SRAM.",
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.external_id} -> {self.user}"


class SramGroup(TimeStampedModel, core_models.UuidMixin):
    """A SRAM collaboration (CO) or a group inside one."""

    class Kind(models.TextChoices):
        COLLABORATION = "co", "Collaboration"
        GROUP = "group", "Group"

    external_id = models.CharField(max_length=255, unique=True)
    display_name = models.CharField(max_length=255)
    urn = models.CharField(
        max_length=255,
        blank=True,
        help_text="SRAM global URN: '<organisation>:<co>' or '<organisation>:<co>:<group>'.",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    description = models.TextField(blank=True)
    labels = models.JSONField(default=list, blank=True)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="sram_groups", blank=True
    )
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Last SCIM Group resource received from SRAM.",
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.display_name} ({self.urn or self.external_id})"

    @property
    def urn_parts(self) -> list[str]:
        return [part for part in self.urn.split(":") if part] if self.urn else []

    @property
    def organisation_short_name(self) -> str:
        parts = self.urn_parts
        return parts[0] if parts else ""
