import django_filters
from django.conf import settings
from django.core import exceptions
from django.db.models import Q
from django import forms
from django.utils import timezone
from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class InvoiceFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')
    state = django_filters.MultipleChoiceFilter(choices=models.Invoice.States.CHOICES)

    class Meta(object):
        model = models.Invoice
        fields = ('year', 'month')


class PaymentDetailsFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')

    class Meta(object):
        model = models.PaymentDetails
        fields = '__all__'


class AccountingStartDateFilter(core_filters.BaseExternalFilter):
    def filter(self, request, queryset, view):

        if not settings.WALDUR_INVOICES['ENABLE_ACCOUNTING_START_DATE']:
            return queryset

        value = request.query_params.get('accounting_is_running')
        boolean_field = forms.NullBooleanField()

        try:
            value = boolean_field.to_python(value)
        except exceptions.ValidationError:
            value = None

        if value is None:
            return queryset

        query = Q(accounting_start_date__gt=timezone.now())

        if value:
            return queryset.exclude(query)
        else:
            return queryset.filter(query)


structure_filters.ExternalCustomerFilterBackend.register(AccountingStartDateFilter())
