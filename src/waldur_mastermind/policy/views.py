from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.serializers import ValidationError

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

    def _check_terminated_project(self, policy):
        """Check if policy scope (project) is terminated and raise error if so"""
        if policy.scope.is_removed:
            raise ValidationError("Cannot update policies for terminated projects.")

    def update(self, request, *args, **kwargs):
        policy = self.get_object()
        self._check_terminated_project(policy)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        policy = self.get_object()
        self._check_terminated_project(policy)
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(parameters=[])
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.ProjectEstimatedCostPolicy.available_actions)
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

    @extend_schema(parameters=[])
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.CustomerEstimatedCostPolicy.available_actions)
        return Response(data, status=status.HTTP_200_OK)


class OfferingEstimatedCostPolicyViewSet(ActionsViewSet):
    queryset = models.OfferingEstimatedCostPolicy.objects.all().order_by("-created")
    serializer_class = serializers.OfferingEstimatedCostPolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.PolicyFilter
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_owner
    ]

    @extend_schema(
        parameters=[],
        description="List available actions for OfferingEstimatedCostPolicy",
    )
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.OfferingEstimatedCostPolicy.available_actions)
        return Response(data, status=status.HTTP_200_OK)


class OfferingUsagePolicyViewSet(ActionsViewSet):
    queryset = models.OfferingUsagePolicy.objects.all().order_by("-created")
    serializer_class = serializers.OfferingUsagePolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.PolicyFilter
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_owner
    ]

    @extend_schema(parameters=[])
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.OfferingUsagePolicy.available_actions)
        return Response(data, status=status.HTTP_200_OK)


class CustomerComponentUsagePolicyViewSet(ActionsViewSet):
    queryset = models.CustomerComponentUsagePolicy.objects.all().order_by("-created")
    serializer_class = serializers.CustomerComponentUsagePolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.CustomerComponentUsagePolicyFilter
    lookup_field = "uuid"
    create_permissions = destroy_permissions = update_permissions = (
        partial_update_permissions
    ) = [structure_permissions.is_staff]

    @extend_schema(parameters=[])
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.CustomerComponentUsagePolicy.available_actions)
        return Response(data, status=status.HTTP_200_OK)
