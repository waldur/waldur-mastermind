from __future__ import unicode_literals

import logging
from django_filters.rest_framework import DjangoFilterBackend

from waldur_core.structure import views as structure_views
from waldur_core.core import views as core_views
from waldur_core.core import validators as core_validators
from waldur_core.structure import filters as structure_filters

from . import models, serializers, filters, executors

logger = logging.getLogger(__name__)


class ServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.RancherService.objects.all()
    serializer_class = serializers.ServiceSerializer


class ServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.RancherServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer
    filter_class = filters.ServiceProjectLinkFilter


class ClusterViewSet(structure_views.BaseResourceViewSet):
    queryset = models.Cluster.objects.all()
    serializer_class = serializers.ClusterSerializer
    filter_class = filters.ClusterFilter
    create_executor = executors.ClusterCreateExecutor
    delete_executor = executors.ClusterDeleteExecutor
    update_executor = executors.ClusterUpdateExecutor
    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.Cluster.States.OK),
    ]


class NodeViewSet(core_views.ActionsViewSet):
    queryset = models.Node.objects.all()
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    serializer_class = serializers.NodeSerializer
    filter_class = filters.NodeFilter
    lookup_field = 'uuid'
    disabled_actions = ['update', 'partial_update']
