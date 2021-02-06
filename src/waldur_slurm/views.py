from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import permissions, viewsets

from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views

from . import executors, filters, models, serializers


class SlurmServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.SlurmService.objects.all()
    serializer_class = serializers.ServiceSerializer


class SlurmServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.SlurmServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer
    filterset_class = filters.SlurmServiceProjectLinkFilter


class AllocationViewSet(structure_views.BaseResourceViewSet):
    queryset = models.Allocation.objects.all()
    serializer_class = serializers.AllocationSerializer
    filterset_class = filters.AllocationFilter

    create_executor = executors.AllocationCreateExecutor
    pull_executor = executors.AllocationPullExecutor

    destroy_permissions = [structure_permissions.is_owner]
    delete_executor = executors.AllocationDeleteExecutor

    partial_update_permissions = update_permissions = [structure_permissions.is_owner]
    update_executor = executors.AllocationUpdateExecutor


class AllocationUsageViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = 'uuid'
    queryset = models.AllocationUsage.objects.all()
    serializer_class = serializers.AllocationUsageSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.AllocationUsageFilter


class AllocationUserUsageViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.AllocationUserUsage.objects.all()
    serializer_class = serializers.AllocationUserUsageSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.AllocationUserUsageFilter


class AssociationViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = 'uuid'
    queryset = models.Association.objects.all()
    serializer_class = serializers.AssociationSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.AssociationFilter


def get_project_allocation_count(project):
    return project.quotas.get(name='nc_allocation_count').usage


structure_views.ProjectCountersView.register_counter(
    'slurm', get_project_allocation_count
)
