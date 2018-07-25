from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, permissions, response, status, viewsets

from waldur_core.structure import filters as structure_filters
from waldur_core.structure import views as structure_views
from waldur_core.structure import permissions as structure_permissions

from . import executors, filters, models, serializers


class SlurmServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.SlurmService.objects.all()
    serializer_class = serializers.ServiceSerializer


class SlurmServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.SlurmServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer
    filter_class = filters.SlurmServiceProjectLinkFilter


class AllocationViewSet(structure_views.BaseResourceViewSet):
    queryset = models.Allocation.objects.all()
    serializer_class = serializers.AllocationSerializer
    filter_class = filters.AllocationFilter

    create_executor = executors.AllocationCreateExecutor
    pull_executor = executors.AllocationPullExecutor

    destroy_permissions = [structure_permissions.is_staff]
    delete_executor = executors.AllocationDeleteExecutor

    partial_update_permissions = update_permissions = [structure_permissions.is_owner]
    update_executor = executors.AllocationUpdateExecutor

    @decorators.detail_route(methods=['post'])
    def cancel(self, request, uuid=None):
        allocation = self.get_object()
        allocation.get_backend().cancel_allocation(allocation)
        return response.Response(status=status.HTTP_200_OK)

    cancel_permissions = [structure_permissions.is_owner]


class AllocationUsageViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.AllocationUsage.objects.all()
    serializer_class = serializers.AllocationUsageSerializer
    lookup_field = 'uuid'
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.AllocationUsageFilter


def get_project_allocation_count(project):
    return project.quotas.get(name='nc_allocation_count').usage


structure_views.ProjectCountersView.register_counter('slurm', get_project_allocation_count)
