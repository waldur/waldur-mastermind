from django.conf import settings
from django.db import models

from waldur_core.structure.models import BaseResource


class Job(BaseResource):
    @classmethod
    def get_service_name(cls):
        return "SLURM"

    file = models.FileField(
        "Batch script file", upload_to="slurm_jobs", blank=True, null=True
    )
    user = models.ForeignKey(
        help_text="Reference to user which submitted job",
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
    )
    report = models.JSONField("Job output", blank=True, null=True)
    runtime_state = models.CharField(max_length=100, blank=True)

    @classmethod
    def get_url_name(cls):
        return "slurm-job"
