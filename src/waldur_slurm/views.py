from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import permissions, response, status, viewsets
from rest_framework.decorators import action

from waldur_core.core import executors as core_executors
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views

from . import executors, filters, models, serializers


class AllocationViewSet(structure_views.ResourceViewSet):
    queryset = models.Allocation.objects.all().order_by('name')
    serializer_class = serializers.AllocationSerializer
    filterset_class = filters.AllocationFilter

    create_executor = executors.AllocationCreateExecutor
    update_executor = core_executors.EmptyExecutor
    pull_executor = executors.AllocationPullExecutor

    destroy_permissions = [structure_permissions.is_administrator]
    delete_executor = executors.AllocationDeleteExecutor

    set_limits_permissions = [structure_permissions.is_staff]
    set_limits_serializer_class = serializers.AllocationSetLimitsSerializer

    @action(detail=True, methods=['post'])
    def set_limits(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.AllocationSetLimitsExecutor().execute(instance)
        return response.Response(
            {'status': _('Setting limits was scheduled.')},
            status=status.HTTP_202_ACCEPTED,
        )


class AllocationUserUsageViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.AllocationUserUsage.objects.all().order_by('year', 'month')
    serializer_class = serializers.AllocationUserUsageSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.AllocationUserUsageFilter


class AssociationViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = 'uuid'
    queryset = models.Association.objects.all().order_by('username')
    serializer_class = serializers.AssociationSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.AssociationFilter
