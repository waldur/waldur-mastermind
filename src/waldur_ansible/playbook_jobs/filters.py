from __future__ import unicode_literals

import django_filters

from waldur_core.core import filters as core_filters

from . import models


class AnsibleJobsFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    project = core_filters.URLFilter(view_name='project-detail', name='service_project_link__project__uuid')
    project_uuid = django_filters.UUIDFilter(name='service_project_link__project__uuid')

    class Meta(object):
        model = models.Job
        fields = ['state']
        o = django_filters.OrderingFilter(fields=(
            'name',
            'state',
            'created',
            'modified',
        ))
