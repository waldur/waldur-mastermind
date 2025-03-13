import django_filters
from django_filters.widgets import BooleanWidget
from rest_framework import filters

from waldur_core.core import filters as core_filters

from . import models


class InvoiceFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="customer__uuid")
    state = django_filters.MultipleChoiceFilter(choices=models.Invoice.States.CHOICES)
    start_date = django_filters.DateFilter(field_name="created", lookup_expr="gt")
    end_date = django_filters.DateFilter(field_name="created", lookup_expr="lt")
    min_sum = django_filters.NumberFilter(method="filter_min_sum", label="Min sum")
    max_sum = django_filters.NumberFilter(method="filter_max_sum", label="Max sum")
    o = django_filters.OrderingFilter(fields=("created", "year", "month"))

    def filter_min_sum(self, queryset, name, value):
        ids = [invoice.id for invoice in queryset.all() if invoice.total >= value]
        return queryset.filter(id__in=ids)

    def filter_max_sum(self, queryset, name, value):
        ids = [invoice.id for invoice in queryset.all() if invoice.total <= value]
        return queryset.filter(id__in=ids)

    class Meta:
        model = models.Invoice
        fields = ["created", "year", "month"]


class InvoiceItemFilter(django_filters.FilterSet):
    resource_uuid = django_filters.UUIDFilter(field_name="resource__uuid")
    offering_uuid = django_filters.UUIDFilter(field_name="resource__offering__uuid")
    year = django_filters.NumberFilter(field_name="invoice__year")
    month = django_filters.NumberFilter(field_name="invoice__month")
    project_uuid = django_filters.UUIDFilter(field_name="project__uuid")
    customer_uuid = django_filters.UUIDFilter(field_name="project__customer__uuid")
    credit_uuid = django_filters.UUIDFilter(field_name="credit__uuid")
    start_year = django_filters.NumberFilter(
        field_name="start__year", lookup_expr="exact", label="Start year"
    )
    start_month = django_filters.NumberFilter(
        field_name="start__month", lookup_expr="exact", label="Start month"
    )

    class Meta:
        model = models.InvoiceItem
        fields = [
            "resource_uuid",
            "offering_uuid",
            "year",
            "month",
            "project_uuid",
            "customer_uuid",
            "credit_uuid",
            "start_year",
            "start_month",
        ]


class PaymentProfileFilter(django_filters.FilterSet):
    organization = core_filters.URLFilter(
        view_name="customer-detail", field_name="organization__uuid"
    )
    organization_uuid = django_filters.UUIDFilter(field_name="organization__uuid")
    payment_type = django_filters.MultipleChoiceFilter(
        choices=models.PaymentType.CHOICES
    )
    o = django_filters.OrderingFilter(fields=("name", "payment_type", "is_active"))
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)

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
        view_name="payment-profile-detail", field_name="profile__uuid"
    )
    profile_uuid = django_filters.UUIDFilter(field_name="profile__uuid")

    class Meta:
        model = models.Payment
        fields = ["date_of_payment"]


class CustomerCreditFilter(django_filters.FilterSet):
    customer_uuid = django_filters.UUIDFilter(field_name="customer__uuid")
    customer_name = django_filters.CharFilter(
        field_name="customer__name", lookup_expr="icontains"
    )
    customer_slug = django_filters.CharFilter(
        field_name="customer__slug", lookup_expr="exact"
    )
    o = django_filters.OrderingFilter(
        fields=(
            ("customer__name", "customer_name"),
            ("value", "value"),
            ("end_date", "end_date"),
            ("expected_consumption", "expected_consumption"),
        ),
    )

    class Meta:
        model = models.CustomerCredit
        fields = []


class ProjectCreditFilter(django_filters.FilterSet):
    project_uuid = django_filters.UUIDFilter(field_name="project__uuid")
    project_name = django_filters.CharFilter(
        field_name="project__name", lookup_expr="icontains"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="project__customer__uuid")
    customer_name = django_filters.CharFilter(
        field_name="project__customer__name", lookup_expr="icontains"
    )
    customer_slug = django_filters.CharFilter(
        field_name="project__customer__slug", lookup_expr="exact"
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("project__name", "project_name"),
            ("value", "value"),
            ("end_date", "end_date"),
            ("expected_consumption", "expected_consumption"),
        ),
    )

    class Meta:
        model = models.ProjectCredit
        fields = []
