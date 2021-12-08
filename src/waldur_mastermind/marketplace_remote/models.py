from django.db import models
from model_utils import FieldTracker

from waldur_core.core.mixins import ReviewMixin
from waldur_core.core.models import UuidMixin
from waldur_core.structure.models import Project, ProjectDetailsMixin
from waldur_mastermind.marketplace.models import Offering


class ProjectUpdateRequest(ProjectDetailsMixin, UuidMixin, ReviewMixin):
    class Meta:
        ordering = ['created']

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='+')
    offering = models.ForeignKey(Offering, on_delete=models.CASCADE, related_name='+')
    tracker = FieldTracker()

    class Permissions:
        customer_path = 'offering__customer'
        project_path = 'project'
