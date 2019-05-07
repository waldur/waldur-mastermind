import json

import django_filters
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from django_filters.widgets import BooleanWidget
from rest_framework import exceptions as rf_exceptions

from waldur_core.core import filters as core_filters
from waldur_core.structure import models as structure_models

from . import models


class ServiceProviderFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')
    o = django_filters.OrderingFilter(fields=(('customer__name', 'customer_name'),))

    class Meta(object):
        model = models.ServiceProvider
        fields = []


class OfferingFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')
    project_uuid = django_filters.UUIDFilter(method='filter_project')
    allowed_customer_uuid = django_filters.UUIDFilter(name='customer__uuid', method='filter_allowed_customer')
    attributes = django_filters.CharFilter(name='attributes', method='filter_attributes')
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[(representation, representation) for db_value, representation in models.Offering.States.CHOICES],
        choice_mappings={representation: db_value for db_value, representation in models.Offering.States.CHOICES},
    )
    category_uuid = django_filters.UUIDFilter(name='category__uuid')
    billable = django_filters.BooleanFilter(widget=BooleanWidget)
    shared = django_filters.BooleanFilter(widget=BooleanWidget)
    o = django_filters.OrderingFilter(fields=('name', 'created'))

    def filter_allowed_customer(self, queryset, name, value):
        return queryset.filter_for_customer(value)

    def filter_project(self, queryset, name, value):
        return queryset.filter_for_project(value)

    def filter_attributes(self, queryset, name, value):
        try:
            value = json.loads(value)
        except ValueError:
            raise rf_exceptions.ValidationError(_('Filter attribute is not valid json.'))

        if not isinstance(value, dict):
            raise rf_exceptions.ValidationError(_('Filter attribute should be an dict.'))

        for k, v in value.items():
            if isinstance(v, list):
                # If a filter value is a list, use multiple choice.
                queryset = queryset.filter(**{'attributes__{key}__has_any_keys'.format(key=k): v})
            else:
                queryset = queryset.filter(attributes__contains={k: v})
        return queryset

    class Meta(object):
        model = models.Offering
        fields = ['shared', 'type']


class OfferingCustomersFilterBackend(DjangoFilterBackend):
    def filter_queryset(self, request, queryset, view):
        return queryset.filter_for_user(request.user)


class ScreenshotFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(view_name='marketplace-offering-detail', name='offering__uuid')
    offering_uuid = django_filters.UUIDFilter(name='offering__uuid')

    o = django_filters.OrderingFilter(fields=('name', 'created'))

    class Meta(object):
        model = models.Screenshot
        fields = []


class OrderFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(view_name='customer-detail', name='project__customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='project__customer__uuid')
    project = core_filters.URLFilter(view_name='project-detail', name='project__uuid')
    project_uuid = django_filters.UUIDFilter(name='project__uuid')
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[(representation, representation) for db_value, representation in models.Order.States.CHOICES],
        choice_mappings={representation: db_value for db_value, representation in models.Order.States.CHOICES},
    )
    o = django_filters.OrderingFilter(fields=('created', 'approved_at', 'total_cost', 'state'))

    class Meta(object):
        model = models.Order
        fields = []


class OrderItemFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(view_name='marketplace-offering-detail', name='offering__uuid')
    offering_uuid = django_filters.UUIDFilter(name='offering__uuid')
    project_uuid = django_filters.UUIDFilter(name='order__project__uuid')
    category_uuid = django_filters.UUIDFilter(name='offering__category__uuid')
    provider_uuid = django_filters.UUIDFilter(name='offering__customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='order__project__customer__uuid')
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[(representation, representation) for db_value, representation in models.OrderItem.States.CHOICES],
        choice_mappings={representation: db_value for db_value, representation in models.OrderItem.States.CHOICES},
    )

    order = core_filters.URLFilter(view_name='marketplace-order-detail', name='order__uuid')
    order_uuid = django_filters.UUIDFilter(name='order__uuid')

    resource = core_filters.URLFilter(view_name='marketplace-resource-detail', name='resource__uuid')
    resource_uuid = django_filters.UUIDFilter(name='resource__uuid')

    o = django_filters.OrderingFilter(fields=('created',))

    class Meta(object):
        model = models.OrderItem
        fields = []


class ResourceFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(view_name='marketplace-offering-detail', name='offering__uuid')
    offering_uuid = django_filters.UUIDFilter(name='offering__uuid')
    offering_type = django_filters.CharFilter(name='offering__type')
    project_uuid = django_filters.UUIDFilter(name='project__uuid')
    project_name = django_filters.CharFilter(name='project__name')
    customer_uuid = django_filters.UUIDFilter(name='project__customer__uuid')
    category_uuid = django_filters.UUIDFilter(name='offering__category__uuid')
    provider_uuid = django_filters.UUIDFilter(name='offering__customer__uuid')
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[(representation, representation) for db_value, representation in models.Resource.States.CHOICES],
        choice_mappings={representation: db_value for db_value, representation in models.Resource.States.CHOICES},
    )
    o = django_filters.OrderingFilter(fields=('name', 'created',))

    class Meta(object):
        model = models.Resource
        fields = []


class ResourceScopeFilterBackend(core_filters.GenericKeyFilterBackend):

    def get_related_models(self):
        return []

    def get_field_name(self):
        return 'scope'


class PlanFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(view_name='marketplace-offering-detail', name='offering__uuid')
    offering_uuid = django_filters.UUIDFilter(name='offering__uuid')

    class Meta(object):
        model = models.Plan
        fields = []


class CategoryComponentUsageScopeFilterBackend(core_filters.GenericKeyFilterBackend):

    def get_related_models(self):
        return [structure_models.Project, structure_models.Customer]

    def get_field_name(self):
        return 'scope'


class CategoryComponentUsageFilter(django_filters.FilterSet):
    class Meta(object):
        model = models.CategoryComponentUsage
        fields = []

    date_before = django_filters.DateFilter(name='date', lookup_expr='lte')
    date_after = django_filters.DateFilter(name='date', lookup_expr='gte')


class ComponentUsageFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(view_name='marketplace-resource-detail', name='resource__uuid')
    resource_uuid = django_filters.UUIDFilter(name='resource__uuid')
    offering_uuid = django_filters.UUIDFilter(name='resource__offering__uuid')
    project_uuid = django_filters.UUIDFilter(name='resource__project__uuid')
    customer_uuid = django_filters.UUIDFilter(name='resource__project__customer__uuid')
    date_before = django_filters.DateFilter(name='date', lookup_expr='lte')
    date_after = django_filters.DateFilter(name='date', lookup_expr='gte')
    type = django_filters.CharFilter(name='component__type')

    class Meta(object):
        model = models.ComponentUsage
        fields = []


class OfferingFileFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(view_name='marketplace-offering-detail', name='offering__uuid')
    offering_uuid = django_filters.UUIDFilter(name='offering__uuid')

    o = django_filters.OrderingFilter(fields=('name', 'created'))

    class Meta(object):
        model = models.OfferingFile
        fields = []
