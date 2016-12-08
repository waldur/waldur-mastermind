import django_filters

from nodeconductor.core import filters as core_filters
from nodeconductor.structure import models as structure_models

from . import models


class IssueFilter(django_filters.FilterSet):
    summary = django_filters.CharFilter(lookup_type='icontains')
    customer = core_filters.UUIDFilter(name='customer__uuid')
    project = core_filters.UUIDFilter(name='project__uuid')
    reporter = core_filters.UUIDFilter(name='reporter__uuid')

    class Meta(object):
        model = models.Issue
        fields = [
            'key',
            'type',
            'status',
        ]
        order_by = [
            'created',
            'updated',
            # desc
            '-created',
            '-updated',
        ]


class IssueResourceFilterBackend(core_filters.GenericKeyFilterBackend):

    def get_related_models(self):
        return structure_models.ResourceMixin.get_all_models()

    def get_field_name(self):
        return 'resource'
