import django_filters

from nodeconductor.core.filters import UUIDFilter, URLFilter

from . import models


class InvoiceFilter(django_filters.FilterSet):
    customer_uuid = UUIDFilter(name='customer__uuid')
    state = django_filters.MultipleChoiceFilter(choices=models.Invoice.States.CHOICES)

    class Meta(object):
        model = models.Invoice
        fields = ('customer_uuid', 'state', 'year', 'month')


class PaymentDetailsFilter(django_filters.FilterSet):
    customer = UUIDFilter(name='customer__uuid')
    customer_url = URLFilter(
        view_name='customer-detail',
        name='customer__uuid',
    )

    class Meta(object):
        model = models.PaymentDetails
        fields = [
            'customer',
            'customer_url',
        ]
