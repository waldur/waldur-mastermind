from __future__ import unicode_literals

from nodeconductor.core import filters as core_filters
from nodeconductor.cost_tracking import models


class PriceEstimateScopeFilterBackend(core_filters.GenericKeyFilterBackend):

    def get_related_models(self):
        return models.PriceEstimate.get_estimated_models()

    def get_field_name(self):
        return 'scope'
