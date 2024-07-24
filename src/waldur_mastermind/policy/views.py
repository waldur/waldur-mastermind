from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core.views import ActionsViewSet
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions

from . import filters, models, serializers


class ProjectEstimatedCostPolicyViewSet(ActionsViewSet):
    queryset = models.ProjectEstimatedCostPolicy.objects.all().order_by("-created")
    serializer_class = serializers.ProjectEstimatedCostPolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.ProjectEstimatedCostPolicyFilter
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_owner
    ]

    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = [
            action.__name__
            for action in models.ProjectEstimatedCostPolicy.available_actions
        ]
        return Response(data, status=status.HTTP_200_OK)


class CustomerEstimatedCostPolicyViewSet(ActionsViewSet):
    queryset = models.CustomerEstimatedCostPolicy.objects.all().order_by("-created")
    serializer_class = serializers.CustomerEstimatedCostPolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.CustomerEstimatedCostPolicyFilter
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_staff
    ]

    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = [
            action.__name__
            for action in models.CustomerEstimatedCostPolicy.available_actions
        ]
        return Response(data, status=status.HTTP_200_OK)
