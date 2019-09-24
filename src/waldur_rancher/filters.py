from __future__ import unicode_literals

import django_filters

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class ServiceProjectLinkFilter(structure_filters.BaseServiceProjectLinkFilter):
    service = core_filters.URLFilter(view_name='rancher-detail', name='service__uuid')

    class Meta(structure_filters.BaseServiceProjectLinkFilter.Meta):
        model = models.RancherServiceProjectLink


class ClusterFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Cluster


class NodeFilter(django_filters.FilterSet):
    cluster_uuid = django_filters.UUIDFilter(name='cluster__uuid')

    class Meta(object):
        model = models.Node
        fields = ('cluster_uuid',)
