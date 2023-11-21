import django_filters
from django.contrib.auth import get_user_model
from django.db.models import Q

from waldur_core.core import filters as core_filters

from . import models

User = get_user_model()


class CallManagerFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name='customer-detail', field_name='customer__uuid'
    )
    customer_uuid = django_filters.UUIDFilter(field_name='customer__uuid')
    customer_keyword = django_filters.CharFilter(method='filter_customer_keyword')
    o = django_filters.OrderingFilter(fields=(('customer__name', 'customer_name'),))

    class Meta:
        model = models.CallManager
        fields = []

    def filter_customer_keyword(self, queryset, name, value):
        return queryset.filter(
            Q(customer__name__icontains=value)
            | Q(customer__abbreviation__icontains=value)
            | Q(customer__native_name__icontains=value)
        )
