import django_filters

from waldur_core.core import filters as core_filters

from . import models


class PaymentFilter(django_filters.FilterSet):
    class Meta:
        model = models.Payment
        fields = ('customer',)

    customer = django_filters.UUIDFilter(
        field_name='customer__uuid',
        distinct=True,
    )


class InvoiceFilter(django_filters.FilterSet):
    class Meta:
        model = models.Invoice
        fields = ('customer', 'year', 'month')

    customer = core_filters.URLFilter(view_name='customer-detail', field_name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(field_name='customer__uuid')
    state = django_filters.MultipleChoiceFilter(choices=models.Invoice.States.CHOICES)
