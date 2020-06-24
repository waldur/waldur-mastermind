import django_filters
from rest_framework import filters

from waldur_core.core import filters as core_filters

from . import models


class InvoiceFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name='customer-detail', field_name='customer__uuid'
    )
    customer_uuid = django_filters.UUIDFilter(field_name='customer__uuid')
    state = django_filters.MultipleChoiceFilter(choices=models.Invoice.States.CHOICES)
    o = django_filters.OrderingFilter(fields=(('year', 'month'),))

    class Meta:
        model = models.Invoice
        fields = ('year', 'month')


class PaymentProfileFilter(django_filters.FilterSet):
    organization = core_filters.URLFilter(
        view_name='customer-detail', field_name='organization__uuid'
    )
    organization_uuid = django_filters.UUIDFilter(field_name='organization__uuid')
    payment_type = django_filters.MultipleChoiceFilter(
        choices=models.PaymentType.CHOICES
    )

    class Meta:
        model = models.PaymentProfile
        fields = []


class PaymentProfileFilterBackend(filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if request.user.is_staff or request.user.is_support:
            return queryset

        return queryset.filter(is_active=True)


class PaymentFilter(django_filters.FilterSet):
    profile = core_filters.URLFilter(
        view_name='payment-profile-detail', field_name='profile__uuid'
    )
    profile_uuid = django_filters.UUIDFilter(field_name='profile__uuid')

    class Meta:
        model = models.Payment
        fields = ['date_of_payment']
