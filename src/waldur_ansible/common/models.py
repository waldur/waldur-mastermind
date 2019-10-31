from django.apps import apps
from django.db import models
from django.utils.lru_cache import lru_cache

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models


class OutputMixin(models.Model):
    output = models.TextField(blank=True)

    class Meta:
        abstract = True


class UuidStrMixin(core_models.UuidMixin):

    class Meta:
        abstract = True

    def __str__(self):
        return '%s: %s' % (self.__class__.__name__, self.uuid.hex)


class ApplicationModel(structure_models.StructureModel):
    class Meta:
        abstract = True

    @classmethod
    @lru_cache(maxsize=1)
    def get_application_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]
