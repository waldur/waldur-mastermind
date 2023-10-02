import django_filters
from django.db.models import Q

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
    start_date = django_filters.DateFilter(field_name='start_date', lookup_expr='gt')
    end_date = django_filters.DateFilter(field_name='end_date', lookup_expr='lt')
    discount_type = django_filters.CharFilter(field_name='discount_type')
    state = django_filters.MultipleChoiceFilter(choices=models.Campaign.States.CHOICES)
    o = django_filters.OrderingFilter(
        fields=(
            'start_date',
            'end_date',
        )
    )
    query = django_filters.CharFilter(method='filter_query')

    def filter_query(self, queryset, name, value):
        return queryset.filter(Q(name__icontains=value) | Q(coupon__icontains=value))
