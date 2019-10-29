import django_filters
from waldur_core.core import filters as core_filters

from . import models


class InvoiceFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(view_name='customer-detail', field_name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(field_name='customer__uuid')
    state = django_filters.MultipleChoiceFilter(choices=models.Invoice.States.CHOICES)
    o = django_filters.OrderingFilter(fields=(('year', 'month'),))

    class Meta:
        model = models.Invoice
        fields = ('year', 'month')
