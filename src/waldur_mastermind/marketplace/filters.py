import json

import django_filters
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions as rf_exceptions

from waldur_core.core import filters as core_filters

from . import models


class ServiceProviderFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')

    class Meta(object):
        model = models.ServiceProvider
        fields = []


class OfferingFilter(django_filters.FilterSet):
    provider = core_filters.URLFilter(view_name='marketplace-service-provider-detail',
                                      name='provider__uuid')
    provider_uuid = django_filters.UUIDFilter(name='provider__uuid')
    attributes = django_filters.CharFilter(name='attributes', method='filter_attributes')

    def filter_attributes(self, queryset, name, value):
        try:
            value = json.loads(value)
        except ValueError:
            raise rf_exceptions.ValidationError(_('Filter attribute is not valid.'))

        for k, v in value.items():
            queryset = queryset.filter(attributes__contains={k: v})
        return queryset

    class Meta(object):
        model = models.Offering
        fields = ['attributes']


class ScreenshotFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(view_name='marketplace-offering-detail', name='offering__uuid')
    offering_uuid = django_filters.UUIDFilter(name='offering__uuid')

    o = django_filters.OrderingFilter(fields=(
        ('name', 'name'),
        ('description', 'description'),
        ('created', 'created'),
        ('modified', 'modified'),
    ))

    class Meta(object):
        model = models.Screenshots
        fields = []


class OrderFilter(django_filters.FilterSet):
    project = core_filters.URLFilter(view_name='project-detail', name='project__uuid')
    project_uuid = django_filters.UUIDFilter(name='project__uuid')

    o = django_filters.OrderingFilter(fields=(
        ('created_by', 'created_by'),
        ('approved_at', 'approved_at'),
        ('total_cost', 'total_cost'),
    ))

    class Meta(object):
        model = models.Order
        fields = ['state']


class ItemFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(view_name='marketplace-offering-detail', name='offering__uuid')
    offering_uuid = django_filters.UUIDFilter(name='offering__uuid')

    order = core_filters.URLFilter(view_name='marketplace-order-detail', name='order__uuid')
    order_uuid = django_filters.UUIDFilter(name='order__uuid')

    class Meta(object):
        model = models.Item
        fields = []
