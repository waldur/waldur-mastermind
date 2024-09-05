from django.db import models
from django.utils.translation import gettext_lazy as _

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models


class BaseImage(structure_models.ServiceProperty):
    min_disk = models.PositiveIntegerField(
        default=0, help_text=_("Minimum disk size in MiB")
    )
    min_ram = models.PositiveIntegerField(
        default=0, help_text=_("Minimum memory size in MiB")
    )

    class Meta(structure_models.ServiceProperty.Meta):
        abstract = True

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + ("min_disk", "min_ram")


class BaseVolumeType(core_models.DescribableMixin, structure_models.ServiceProperty):
    class Meta:
        unique_together = ("settings", "backend_id")
        abstract = True

    def __str__(self):
        return self.name
