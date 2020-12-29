from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_slurm import mixins as slurm_mixins
from waldur_slurm import utils


class SlurmService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project, related_name='+', through='SlurmServiceProjectLink'
    )

    class Meta:
        unique_together = ('customer', 'settings')
        verbose_name = _('SLURM provider')
        verbose_name_plural = _('SLURM providers')

    @classmethod
    def get_url_name(cls):
        return 'slurm'


class SlurmServiceProjectLink(structure_models.ServiceProjectLink):
    service = models.ForeignKey(on_delete=models.CASCADE, to=SlurmService)

    class Meta(structure_models.ServiceProjectLink.Meta):
        verbose_name = _('SLURM provider project link')
        verbose_name_plural = _('SLURM provider project links')

    @classmethod
    def get_url_name(cls):
        return 'slurm-spl'


SLURM_ALLOCATION_REGEX = 'a-zA-Z0-9-_'
SLURM_ALLOCATION_NAME_MAX_LEN = 34


class Allocation(structure_models.NewResource):
    service_project_link = models.ForeignKey(
        SlurmServiceProjectLink, related_name='allocations', on_delete=models.PROTECT
    )
    is_active = models.BooleanField(default=True)
    tracker = FieldTracker()

    cpu_limit = models.BigIntegerField(
        default=settings.WALDUR_SLURM['DEFAULT_LIMITS']['CPU']
    )
    cpu_usage = models.BigIntegerField(default=0)

    gpu_limit = models.BigIntegerField(
        default=settings.WALDUR_SLURM['DEFAULT_LIMITS']['GPU']
    )
    gpu_usage = models.BigIntegerField(default=0)

    ram_limit = models.BigIntegerField(
        default=settings.WALDUR_SLURM['DEFAULT_LIMITS']['RAM']
    )
    ram_usage = models.BigIntegerField(default=0)

    @classmethod
    def get_url_name(cls):
        return 'slurm-allocation'

    def usage_changed(self):
        return any(self.tracker.has_changed(field) for field in utils.FIELD_NAMES)

    @classmethod
    def get_backend_fields(cls):
        return super(Allocation, cls).get_backend_fields() + (
            'cpu_usage',
            'gpu_usage',
            'ram_usage',
        )


class AllocationUsage(slurm_mixins.UsageMixin, core_models.UuidMixin):
    class Permissions:
        customer_path = 'allocation__service_project_link__project__customer'
        project_path = 'allocation__service_project_link__project'
        service_path = 'allocation__service_project_link__service'

    class Meta:
        ordering = ['allocation']

    allocation = models.ForeignKey(on_delete=models.CASCADE, to=Allocation)

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )

    tracker = FieldTracker()

    def __str__(self):
        return "%s [%s-%s]" % (self.allocation.name, self.month, self.year)

    def __repr__(self) -> str:
        return self.__str__()


class AllocationUserUsage(slurm_mixins.UsageMixin):
    """
    Allocation usage per user. This model is responsible for the allocation usage definition for particular user.
    """

    allocation_usage = models.ForeignKey(to=AllocationUsage, on_delete=models.CASCADE)

    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE, blank=True, null=True
    )

    username = models.CharField(max_length=32)

    def __str__(self):
        return "%s: %s" % (self.username, self.allocation_usage.allocation.name)

    def __repr__(self) -> str:
        return self.__str__()
