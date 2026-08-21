from typing import cast

from django.conf import settings
from django.db import models
from django_fsm import FSMField
from model_utils import FieldTracker
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core import models as core_models
from waldur_core.core.mixins import ReviewMixin
from waldur_core.structure.models import PROJECT_NAME_LENGTH, Project
from waldur_mastermind.marketplace import models as marketplace_models


class ProjectUpdateRequest(core_models.UuidMixin, ReviewMixin):
    class Meta:
        ordering = ["created", "id"]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    offering = models.ForeignKey(
        marketplace_models.Offering, on_delete=models.CASCADE, related_name="+"
    )
    tracker = cast(FieldInstanceTracker, FieldTracker())
    old_name = models.CharField(max_length=PROJECT_NAME_LENGTH, blank=True)
    new_name = models.CharField(max_length=PROJECT_NAME_LENGTH, blank=True)
    old_description = models.CharField(
        max_length=core_models.DESCRIPTION_LENGTH, blank=True
    )
    new_description = models.CharField(
        max_length=core_models.DESCRIPTION_LENGTH, blank=True
    )
    old_end_date = models.DateField(null=True, blank=True)
    new_end_date = models.DateField(null=True, blank=True)
    old_oecd_fos_2007_code = models.CharField(null=True, blank=True, max_length=5)
    new_oecd_fos_2007_code = models.CharField(null=True, blank=True, max_length=5)
    old_is_industry = models.BooleanField(null=True, blank=True)
    new_is_industry = models.BooleanField(null=True, blank=True)
    created_by = models.ForeignKey[core_models.User](
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name="+",
        blank=True,
        null=True,
    )

    def get_old_oecd_fos_2007_code_display(self):
        return Project.OECD_FOS_2007_CODES_DICT.get(self.old_oecd_fos_2007_code)

    def get_new_oecd_fos_2007_code_display(self):
        return Project.OECD_FOS_2007_CODES_DICT.get(self.new_oecd_fos_2007_code)

    class Permissions:
        customer_path = "offering__customer"
        project_path = "project"


class RemoteSynchronisation(
    core_models.UuidMixin, core_models.ErrorMessageMixin, core_models.TimeStampedModel
):
    class States:
        SCHEDULED = "Scheduled"
        PROCESSING = "Processing"
        OK = "OK"
        ERRED = "Erred"

        CHOICES = (
            (SCHEDULED, SCHEDULED),
            (PROCESSING, PROCESSING),
            (OK, OK),
            (ERRED, ERRED),
        )

    state = FSMField(choices=States.CHOICES, default=States.SCHEDULED, editable=False)

    api_url = models.URLField()
    token = models.CharField(max_length=255)
    remote_organization_uuid = models.UUIDField()
    remote_organization_name = models.CharField(max_length=255)
    local_service_provider = models.ForeignKey(
        marketplace_models.ServiceProvider, on_delete=models.CASCADE
    )
    is_active = models.BooleanField(default=True)
    last_execution = models.DateTimeField(null=True, editable=False)
    last_output = models.TextField(editable=False)

    class Meta:
        unique_together = ("local_service_provider", "remote_organization_uuid")

    class Permissions:
        customer_path = "local_service_provider__customer"

    def __str__(self):
        return f"Remote sync from {self.api_url} / {self.remote_organization_uuid}"


class RemoteLocalCategory(core_models.UuidMixin):
    local_category = models.ForeignKey(
        marketplace_models.Category, on_delete=models.CASCADE
    )
    remote_category = models.UUIDField()
    remote_category_name = models.CharField(max_length=255, default="Unknown")
    remote_synchronisation = models.ForeignKey(
        RemoteSynchronisation, on_delete=models.CASCADE, editable=False
    )

    def __str__(self):
        return f"{self.remote_category} / {self.remote_category_name} -> {self.local_category.title}"
