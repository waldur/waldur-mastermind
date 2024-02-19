import django_filters
from django.contrib.auth import get_user_model
from django.db.models import Q

from waldur_core.core import filters as core_filters

from . import models

User = get_user_model()


class CallManagingOrganisationFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="customer__uuid")
    customer_keyword = django_filters.CharFilter(method="filter_customer_keyword")
    o = django_filters.OrderingFilter(fields=(("customer__name", "customer_name"),))

    class Meta:
        model = models.CallManagingOrganisation
        fields = []

    def filter_customer_keyword(self, queryset, name, value):
        return queryset.filter(
            Q(customer__name__icontains=value)
            | Q(customer__abbreviation__icontains=value)
            | Q(customer__native_name__icontains=value)
        )


class CallFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="manager__customer__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="manager__customer__uuid")
    customer_keyword = django_filters.CharFilter(method="filter_customer_keyword")
    o = django_filters.OrderingFilter(
        fields=("manager__customer__name", "created", "name")
    )

    class Meta:
        model = models.Call
        fields = []

    def filter_customer_keyword(self, queryset, name, value):
        return queryset.filter(
            Q(manager__customer__name__icontains=value)
            | Q(manager__customer__abbreviation__icontains=value)
            | Q(manager__customer__native_name__icontains=value)
        )


class ProposalFilter(django_filters.FilterSet):
    call = core_filters.URLFilter(
        view_name="proposal-public-call-detail", field_name="round__call__uuid"
    )
    round = django_filters.UUIDFilter(field_name="round__uuid")
    customer_uuid = django_filters.UUIDFilter(field_name="round__call__uuid")
    o = django_filters.OrderingFilter(
        fields=("round__call__name", "start_time", "end_time", "state")
    )

    class Meta:
        model = models.Proposal
        fields = []


class ReviewFilter(django_filters.FilterSet):
    proposal = core_filters.URLFilter(
        view_name="proposal-proposal-detail", field_name="proposal__uuid"
    )
    o = django_filters.OrderingFilter(fields=("created", "state"))

    class Meta:
        model = models.Review
        fields = ["state"]


class RequestedOfferingFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering",
    )
    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    call = core_filters.URLFilter(
        view_name="proposal-public-call-detail",
        field_name="call__uuid",
        label="Call",
    )
    call_uuid = django_filters.UUIDFilter(field_name="call__uuid")
    o = django_filters.OrderingFilter(
        fields=("created", "state", "offering__name", "call__name")
    )

    class Meta:
        model = models.RequestedOffering
        fields = ["state"]
