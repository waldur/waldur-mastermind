import django_filters

from waldur_core.configuration import config
from waldur_core.core import filters as core_filters
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_pid import models


class OfferingReferralFilter(django_filters.FilterSet):
    o = django_filters.OrderingFilter(
        fields=(
            "published",
            "relation_type",
            "resource_type",
        )
    )

    class Meta:
        model = models.DataciteReferral
        fields = []


class OfferingReferralScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def is_anonymous_allowed(self):
        return config.ANONYMOUS_USER_CAN_VIEW_OFFERINGS

    def get_related_models(self):
        return [marketplace_models.Offering]

    def get_field_name(self):
        return "scope"
