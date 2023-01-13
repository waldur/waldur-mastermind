import django_filters

from waldur_core.core import filters as core_filters

from . import models


class CampaignFilter(django_filters.FilterSet):
    class Meta:
        model = models.Campaign
        fields = []

    offering = core_filters.URLFilter(
        view_name='marketplace-provider-offering-detail',
        field_name='offering__uuid',
        label='Offering',
    )
    offering_uuid = django_filters.UUIDFilter(field_name='offering__uuid')
    service_provider_uuid = django_filters.UUIDFilter(
        field_name='service_provider__uuid'
    )

    o = django_filters.OrderingFilter(
        fields=(
            'start_date',
            'end_date',
        )
    )
