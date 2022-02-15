from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.media.models import ImageModelMixin
from waldur_core.media.validators import ImageValidator
from waldur_core.structure.models import Customer, CustomerRole, ProjectRole
from waldur_mastermind.marketplace import models as marketplace_models


class Category(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
):
    icon = models.FileField(
        upload_to='marketplace_checklist_category_icons',
        blank=True,
        null=True,
        validators=[ImageValidator],
    )

    def __str__(self):
        return self.name

    class Meta:
        ordering = ('name',)
        verbose_name_plural = "Categories"


class Checklist(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    TimeStampedModel,
):
    category = models.ForeignKey(
        to=Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='checklists',
    )
    customers = models.ManyToManyField(Customer)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ('name',)


class ChecklistCustomerRole(models.Model):
    checklist = models.ForeignKey(
        to=Checklist, on_delete=models.CASCADE, related_name='customer_roles'
    )
    role = CustomerRole()


class ChecklistProjectRole(models.Model):
    checklist = models.ForeignKey(
        to=Checklist, on_delete=models.CASCADE, related_name='project_roles'
    )
    role = ProjectRole()


class Question(core_models.UuidMixin, core_models.DescribableMixin, ImageModelMixin):
    checklist = models.ForeignKey(
        to=Checklist,
        on_delete=models.CASCADE,
        related_name='questions',
    )
    order = models.PositiveIntegerField(default=0)
    category = models.ForeignKey(
        to=marketplace_models.Category,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    correct_answer = models.BooleanField(default=True)
    solution = models.TextField(
        blank=True,
        null=True,
        help_text=_('It is shown when incorrect or N/A answer is chosen'),
    )

    class Meta:
        ordering = (
            'checklist',
            'order',
        )

    def __str__(self):
        return self.description


class Answer(TimeStampedModel):
    user = models.ForeignKey(to=core_models.User, on_delete=models.CASCADE)
    question = models.ForeignKey(to=Question, on_delete=models.CASCADE)
    value = models.BooleanField(null=True)
