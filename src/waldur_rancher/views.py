import functools
import logging
import operator

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, response, status
from rest_framework.exceptions import ValidationError

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import views as structure_views
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.permissions import is_administrator
from waldur_rancher.apps import RancherConfig

from . import models, serializers, filters, executors, exceptions, validators

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

    def destroy(self, request, *args, **kwargs):
        user = self.request.user
        instance = self.get_object()
        executors.ClusterDeleteExecutor.execute(
            instance,
            user=user,
            is_heavy_task=True,
        )
        return response.Response(
            {'detail': _('Deletion was scheduled.')}, status=status.HTTP_202_ACCEPTED)

    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.Cluster.States.OK),
    ]
    destroy_validators = structure_views.ImportableResourceViewSet.destroy_validators + [
        validators.all_cluster_related_vms_can_be_deleted,
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
    destroy_validators = [
        validators.related_vm_can_be_deleted,
    ]

    def perform_create(self, serializer):
        node = serializer.save()
        user = self.request.user
        transaction.on_commit(lambda: executors.NodeCreateExecutor.execute(
            node,
            user=user,
            is_heavy_task=True,
        ))

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        user = self.request.user
        executors.NodeDeleteExecutor.execute(
            instance,
            user=user,
            is_heavy_task=True,
        )
        return response.Response(status=status.HTTP_202_ACCEPTED)

    @decorators.action(detail=True, methods=['post'])
    def link_openstack(self, request, uuid=None):
        node = self.get_object()
        if node.content_type and node.object_id:
            raise ValidationError('Node is already linked.')
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

    @decorators.action(detail=True, methods=['post'])
    def unlink_openstack(self, request, uuid=None):
        node = self.get_object()
        if not node.content_type or not node.object_id:
            raise ValidationError('Node is not linked to any OpenStack instance yet.')
        node.content_type = None
        node.object_id = None
        node.save()
        return response.Response(status=status.HTTP_200_OK)

    unlink_openstack_permissions = [structure_permissions.is_staff]


class CatalogViewSet(core_views.ActionsViewSet):
    queryset = models.Catalog.objects.all()
    serializer_class = serializers.CatalogSerializer
    lookup_field = 'uuid'

    def get_queryset(self):
        settings_uuid = self.request.query_params.get('settings_uuid')
        cluster_uuid = self.request.query_params.get('cluster_uuid')
        if settings_uuid:
            return self.filter_catalogs_for_settings(settings_uuid)
        elif cluster_uuid:
            return self.filter_catalogs_for_cluster(cluster_uuid)
        else:
            return self.filter_visible_catalogs()

    def filter_catalogs_for_settings(self, settings_uuid):
        qs = ServiceSettings.objects.filter(type=RancherConfig.service_name)
        scope = get_object_or_404(qs, uuid=settings_uuid)
        ctype = ContentType.objects.get_for_model(ServiceSettings)
        return self.queryset.filter(content_type=ctype, object_id=scope.id)

    def filter_catalogs_for_cluster(self, cluster_uuid):
        qs = filter_queryset_for_user(
            queryset=models.Cluster.objects.all(),
            user=self.request.user,
        )
        cluster = get_object_or_404(qs, uuid=cluster_uuid)
        return self.queryset.filter(
            Q(content_type=ContentType.objects.get_for_model(models.Cluster),
              object_id=cluster.id) |
            Q(content_type=ContentType.objects.get_for_model(ServiceSettings),
              object_id=cluster.service_project_link.service.settings.id)
        )

    def filter_visible_catalogs(self):
        settings_subquery = self.get_filtered_subquery(
            models.ServiceSettings.objects.filter(type=RancherConfig.service_name))
        clusters_subquery = self.get_filtered_subquery(models.Cluster.objects.all())
        # TODO: Implement project-level catalogs
        visible_scopes = settings_subquery | clusters_subquery
        return self.queryset.filter(visible_scopes)

    def get_filtered_subquery(self, queryset):
        ids = filter_queryset_for_user(
            queryset=queryset,
            user=self.request.user,
        ).values_list('id', flat=True)
        content_type = ContentType.objects.get_for_model(queryset.model)
        return functools.reduce(operator.or_, [
            Q(content_type=content_type, object_id=object_id)
            for object_id in ids
        ])

    @decorators.action(detail=True, methods=['post'])
    def refresh(self, request, uuid=None):
        catalog = self.get_object()
        backend = catalog.get_backend()
        backend.refresh_catalog(catalog)
        return response.Response(status=status.HTTP_200_OK)

    refresh_permissions = [structure_permissions.is_staff]

    def perform_create(self, serializer):
        scope = serializer.validated_data['scope']
        self.check_catalog_permissions(scope)

        if isinstance(scope, ServiceSettings):
            if scope.type != RancherConfig.service_name:
                raise ValidationError('Invalid provider detected.')
        elif not isinstance(scope, models.Cluster):
            # TODO: Implement project-level catalog
            raise ValidationError('Invalid scope provided.')

        catalog = serializer.save()
        backend = catalog.get_backend()
        backend.create_catalog(catalog)

    create_serializer_class = serializers.CatalogCreateSerializer

    def perform_update(self, serializer):
        scope = serializer.instance.scope
        self.check_catalog_permissions(scope)
        catalog = serializer.save()
        backend = catalog.get_backend()
        backend.update_catalog(catalog)

    update_serializer_class = serializers.CatalogUpdateSerializer

    def perform_destroy(self, catalog):
        self.check_catalog_permissions(catalog.scope)
        backend = catalog.get_backend()
        backend.delete_catalog(catalog)
        catalog.delete()

    def check_catalog_permissions(self, scope):
        if isinstance(scope, ServiceSettings) and not self.request.user.is_staff:
            raise ValidationError(_('Only staff is allowed to manage global catalogs.'))
        if isinstance(scope, models.Cluster):
            is_administrator(self.request.user, scope.service_project_link.project)
