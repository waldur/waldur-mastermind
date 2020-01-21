import logging

from django.apps import apps
from django.db import models
from django.utils.lru_cache import lru_cache

logger = logging.getLogger(__name__)


class CoordinatesMixin(models.Model):
    """
    Mixin to add a latitude and longitude fields
    """
    class Meta:
        abstract = True

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)


class IPCoordinatesMixin(CoordinatesMixin):
    detect_coordinates = NotImplemented

    class Meta:
        abstract = True

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]
