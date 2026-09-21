from django.db import models
from django.utils.translation import gettext_lazy as _

from waldur_core.core.models import TimeStampedModel
from waldur_core.logging.models import UuidMixin


class ChangelogImpactAnalysis(UuidMixin, TimeStampedModel):
    class Status:
        PENDING = "pending"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"

        CHOICES = (
            (PENDING, _("Pending")),
            (RUNNING, _("Running")),
            (COMPLETED, _("Completed")),
            (FAILED, _("Failed")),
        )

    current_version = models.CharField(max_length=32)
    target_version = models.CharField(max_length=32)
    status = models.CharField(
        max_length=16,
        choices=Status.CHOICES,
        default=Status.PENDING,
    )
    results = models.JSONField(
        default=dict,
        help_text="Impact analysis results keyed by changelog entry ID.",
    )
    computed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    class Meta:
        unique_together = ("current_version", "target_version")
        verbose_name = "Changelog impact analysis"
        verbose_name_plural = "Changelog impact analyses"

    def __str__(self):
        return f"Impact analysis {self.current_version} → {self.target_version} ({self.status})"
