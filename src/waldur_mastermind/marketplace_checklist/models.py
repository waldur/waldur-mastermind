from django.db import models

from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.structure.models import Project, StructureModel
from waldur_mastermind.marketplace.models import Category


class Checklist(core_models.UuidMixin,
                core_models.NameMixin,
                core_models.DescribableMixin,
                TimeStampedModel):

    def __str__(self):
        return self.name


class Question(core_models.UuidMixin, core_models.DescribableMixin):
    checklist = models.ForeignKey(
        to=Checklist,
        on_delete=models.CASCADE,
        related_name='questions',
    )
    order = models.PositiveIntegerField(default=0)
    category = models.ForeignKey(
        to=Category,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    solution = models.TextField(blank=True, null=True)  # It is shown when No or N/A answer is chosen

    class Meta:
        ordering = ('checklist', 'order',)

    def __str__(self):
        return self.description


class Answer(StructureModel, TimeStampedModel):
    user = models.ForeignKey(to=core_models.User, on_delete=models.SET_NULL, null=True, blank=True)
    question = models.ForeignKey(to=Question, on_delete=models.CASCADE)
    project = models.ForeignKey(to=Project, on_delete=models.CASCADE)
    value = models.NullBooleanField()

    class Permissions:
        project_path = 'project'
        customer_path = 'project__customer'
