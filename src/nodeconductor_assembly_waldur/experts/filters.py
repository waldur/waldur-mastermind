import django_filters

from nodeconductor.core import filters as core_filters

from . import models


class ExpertProviderFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid', label='Customer url')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')

    class Meta(object):
        model = models.ExpertProvider
        fields = ('customer',)


class ExpertRequestFilter(django_filters.FilterSet):
    project = core_filters.URLFilter(view_name='project-detail', name='project__uuid')
    project_uuid = django_filters.UUIDFilter(name='project__uuid')

    class Meta(object):
        model = models.ExpertRequest
        fields = ('project',)
