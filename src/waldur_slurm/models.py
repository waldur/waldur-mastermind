import re

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_slurm import utils

SLURM_ALLOCATION_REGEX = "a-zA-Z0-9-_"
SLURM_ALLOCATION_NAME_MAX_LEN = 34


class UsageMixin(models.Model):
    class Meta:
        abstract = True

    cpu_usage = models.BigIntegerField(default=0)
    ram_usage = models.BigIntegerField(default=0)
    gpu_usage = models.BigIntegerField(default=0)


class Allocation(UsageMixin, structure_models.BaseResource):
    is_active = models.BooleanField(default=True)
    tracker = FieldTracker()

    cpu_limit = models.BigIntegerField(default=0)
    gpu_limit = models.BigIntegerField(default=0)
    ram_limit = models.BigIntegerField(default=0)

    @classmethod
    def get_url_name(cls):
        return "slurm-allocation"

    def usage_changed(self):
        return any(self.tracker.has_changed(field) for field in utils.FIELD_NAMES)

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "cpu_usage",
            "gpu_usage",
            "ram_usage",
        )


class Association(core_models.UuidMixin):
    allocation = models.ForeignKey(
        to=Allocation, on_delete=models.CASCADE, related_name="associations"
    )
    username = models.CharField(
        max_length=128,
        validators=[
            RegexValidator(
                re.compile(core_models.USERNAME_REGEX),
                _("Enter a valid username."),
                "invalid",
            ),
        ],
    )

    def __str__(self):
        return f"{self.allocation.name} <-> {self.username}"


class AllocationUserUsage(UsageMixin):
    """
    Allocation usage per user. This model is responsible for the allocation usage definition for particular user.
    """

    allocation = models.ForeignKey(to=Allocation, on_delete=models.CASCADE)
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, blank=True, null=True
    )

    username = models.CharField(max_length=32)

    def __str__(self):
        return f"{self.username}: {self.allocation.name}"

    def __repr__(self) -> str:
        return self.__str__()
