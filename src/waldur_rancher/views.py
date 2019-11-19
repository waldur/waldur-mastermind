import logging

from django.contrib.contenttypes.models import ContentType
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, response, status

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import views as structure_views
from waldur_core.structure import permissions as structure_permissions

from . import models, serializers, filters, executors

logger = logging.getLogger(__name__)


class ServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.RancherService.objects.all()
    serializer_class = serializers.ServiceSerializer


class ServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.RancherServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer
    filterset_class = filters.ServiceProjectLinkFilter


class ClusterViewSet(structure_views.ImportableResourceViewSet):
    queryset = models.Cluster.objects.all()
    serializer_class = serializers.ClusterSerializer
    filterset_class = filters.ClusterFilter
    delete_executor = executors.ClusterDeleteExecutor
    update_executor = executors.ClusterUpdateExecutor

    def perform_create(self, serializer):
        cluster = serializer.save()
        user = self.request.user
        nodes = serializer.validated_data.get('node_set')
        executors.ClusterCreateExecutor.execute(
            cluster,
            nodes=nodes,
            user=user,
            is_heavy_task=True,
        )

    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.Cluster.States.OK),
    ]
    importable_resources_backend_method = 'get_clusters_for_import'
    importable_resources_serializer_class = serializers.ClusterImportableSerializer
    import_resource_serializer_class = serializers.ClusterImportSerializer


class NodeViewSet(core_views.ActionsViewSet):
    queryset = models.Node.objects.all()
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    serializer_class = serializers.NodeSerializer
    filterset_class = filters.NodeFilter
    lookup_field = 'uuid'
    disabled_actions = ['update', 'partial_update']
    create_permissions = [structure_permissions.is_staff]

    @decorators.action(detail=True, methods=['post'])
    def link_openstack(self, request, uuid=None):
        node = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.validated_data['instance']
        node.content_type = ContentType.objects.get_for_model(instance)
        node.object_id = instance.id
        node.name = instance.name
        node.save()
        return response.Response(status=status.HTTP_200_OK)

    link_openstack_permissions = [structure_permissions.is_staff]
    link_openstack_serializer_class = serializers.LinkOpenstackSerializer
