import django_filters

from nodeconductor.core.filters import URLFilter

from . import models


class InvoiceFilter(django_filters.FilterSet):
    customer = URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')
    state = django_filters.MultipleChoiceFilter(choices=models.Invoice.States.CHOICES)

    class Meta(object):
        model = models.Invoice
        fields = ('year', 'month')


class PaymentDetailsFilter(django_filters.FilterSet):
    customer = URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')

    class Meta(object):
        model = models.PaymentDetails
