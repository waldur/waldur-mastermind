import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, response, status
from rest_framework.exceptions import ValidationError

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import views as structure_views
from waldur_core.structure import permissions as structure_permissions

from . import models, serializers, filters, executors, exceptions

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

        for node_data in nodes:
            node_data['cluster'] = cluster
            models.Node.objects.create(**node_data)

        transaction.on_commit(lambda: executors.ClusterCreateExecutor.execute(
            cluster,
            user=user,
            is_heavy_task=True,
        ))

    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.Cluster.States.OK),
    ]
    importable_resources_backend_method = 'get_clusters_for_import'
    importable_resources_serializer_class = serializers.ClusterImportableSerializer
    import_resource_serializer_class = serializers.ClusterImportSerializer
    pull_executor = executors.ClusterPullExecutor

    @decorators.action(detail=True, methods=['get'])
    def kubeconfig_file(self, request, uuid=None):
        cluster = self.get_object()
        backend = cluster.get_backend()
        try:
            config = backend.get_kubeconfig_file(cluster)
        except exceptions.RancherException:
            raise ValidationError('Unable to get kubeconfig file.')

        return response.Response({'config': config}, status=status.HTTP_200_OK)

    kubeconfig_file_validators = [core_validators.StateValidator(models.Cluster.States.OK)]


class NodeViewSet(core_views.ActionsViewSet):
    queryset = models.Node.objects.all()
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    serializer_class = serializers.NodeSerializer
    create_serializer_class = serializers.CreateNodeSerializer
    filterset_class = filters.NodeFilter
    lookup_field = 'uuid'
    disabled_actions = ['update', 'partial_update']
    create_permissions = [structure_permissions.is_staff]

    def perform_create(self, serializer):
        node = serializer.save()
        user = self.request.user
        transaction.on_commit(lambda: executors.NodeCreateExecutor.execute(
            node,
            user=user,
            is_heavy_task=True,
        ))

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
