import django_filters

from nodeconductor.core import filters as core_filters

from . import models


class ExportProviderFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid', label='Customer url')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')

    class Meta(object):
        model = models.ExpertProvider
        fields = ('customer',)
