from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import permissions, viewsets

from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views

from . import executors, filters, models, serializers


class AllocationViewSet(structure_views.ResourceViewSet):
    queryset = models.Allocation.objects.all()
    serializer_class = serializers.AllocationSerializer
    filterset_class = filters.AllocationFilter

    create_executor = executors.AllocationCreateExecutor
    pull_executor = executors.AllocationPullExecutor

    destroy_permissions = [structure_permissions.is_owner]
    delete_executor = executors.AllocationDeleteExecutor

    partial_update_permissions = update_permissions = [structure_permissions.is_owner]
    update_executor = executors.AllocationUpdateExecutor


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
