import django_filters

from waldur_core.core import filters as core_filters

from . import models


class ExpertProviderFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')

    class Meta(object):
        model = models.ExpertProvider
        fields = []


class ExpertRequestFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    project = core_filters.URLFilter(view_name='project-detail', name='project__uuid')
    project_uuid = django_filters.UUIDFilter(name='project__uuid')
    o = django_filters.OrderingFilter(fields=(
        ('name', 'name'),
        ('type', 'type'),
        ('state', 'state'),
        ('project__customer__name', 'customer_name'),
        ('project__name', 'project_name'),
        ('created', 'created'),
        ('modified', 'modified'),
    ))

    class Meta(object):
        model = models.ExpertRequest
        fields = ['state']


class ExpertBidFilter(django_filters.FilterSet):
    request = core_filters.URLFilter(view_name='expert-request-detail', name='request__uuid')
    request_uuid = django_filters.UUIDFilter(name='request__uuid')
    customer = core_filters.URLFilter(view_name='customer-detail', name='team__customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='team__customer__uuid')

    class Meta(object):
        model = models.ExpertBid
        fields = []
