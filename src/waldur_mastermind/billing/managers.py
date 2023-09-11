from django.db import models as django_models

from waldur_core.core import managers as core_managers


class PriceEstimateManager(core_managers.GenericKeyMixin, django_models.Manager):
    def get_available_models(self):
        return self.model.get_estimated_models()
