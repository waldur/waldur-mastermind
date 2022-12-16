import django_filters

from waldur_core.core import filters as core_filters


class CampaignFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(
        view_name='marketplace-provider-offering-detail',
        field_name='offering__uuid',
        label='Offering',
    )
    offering_uuid = django_filters.UUIDFilter(field_name='offering__uuid')
    is_active = django_filters.BooleanFilter(field_name='is_active')

    o = django_filters.OrderingFilter(
        fields=(
            'start_date',
            'end_date',
        )
    )
