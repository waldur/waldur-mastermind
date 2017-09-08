from django.db import models as django_models

from nodeconductor.core import managers as core_managers
from nodeconductor.cost_tracking import managers as cost_managers


class PriceEstimateManager(core_managers.GenericKeyMixin,
                           cost_managers.UserFilterMixin,
                           django_models.Manager):

    def get_available_models(self):
        return self.model.get_estimated_models()
