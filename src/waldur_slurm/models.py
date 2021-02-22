from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
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


class UsageMixin(models.Model):
    class Meta:
        abstract = True

    cpu_usage = models.BigIntegerField(default=0)
    ram_usage = models.BigIntegerField(default=0)
    gpu_usage = models.BigIntegerField(default=0)


class Allocation(UsageMixin, structure_models.NewResource):
    service_project_link = models.ForeignKey(
        SlurmServiceProjectLink, related_name='allocations', on_delete=models.PROTECT
    )
    is_active = models.BooleanField(default=True)
    tracker = FieldTracker()

    cpu_limit = models.BigIntegerField(
        default=settings.WALDUR_SLURM['DEFAULT_LIMITS']['CPU']
    )
    gpu_limit = models.BigIntegerField(
        default=settings.WALDUR_SLURM['DEFAULT_LIMITS']['GPU']
    )
    ram_limit = models.BigIntegerField(
        default=settings.WALDUR_SLURM['DEFAULT_LIMITS']['RAM']
    )

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


class Association(core_models.UuidMixin):
    allocation = models.ForeignKey(
        to=Allocation, on_delete=models.CASCADE, related_name='associations'
    )
    username = models.CharField(max_length=128)

    def __str__(self):
        return '%s <-> %s' % (self.allocation.name, self.username)


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
        return "%s: %s" % (self.username, self.allocation.name)

    def __repr__(self) -> str:
        return self.__str__()
