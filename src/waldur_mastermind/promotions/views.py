from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory
from waldur_core.structure import filters as structure_filters
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.promotions import filters, models, serializers, validators


class CampaignViewSet(core_views.ActionsViewSet):
    queryset = models.Campaign.objects.filter().order_by('start_date')
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    lookup_field = 'uuid'
    filterset_class = filters.CampaignFilter
    serializer_class = serializers.CampaignSerializer

    destroy_permissions = (
        update_permissions
    ) = activate_permissions = terminate_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_CAMPAIGN, ['service_provider.customer']
        )
    ]
    destroy_validators = [validators.check_resources]
    update_validators = [
        core_validators.StateValidator(
            models.Campaign.States.ACTIVE, models.Campaign.States.DRAFT
        )
    ]
    disabled_actions = ['partial_update']

    @action(detail=True, methods=['post'])
    def activate(self, request, uuid=None):
        campaign = self.get_object()
        campaign.activate()
        campaign.save()
        return Response('Campaign has been activated', status=status.HTTP_200_OK)

    activate_validators = [core_validators.StateValidator(models.Campaign.States.DRAFT)]

    @action(detail=True, methods=['post'])
    def terminate(self, request, uuid=None):
        campaign = self.get_object()
        campaign.terminate()
        campaign.save()
        return Response('Campaign has been terminated', status=status.HTTP_200_OK)

    terminate_validators = [
        core_validators.StateValidator(
            models.Campaign.States.ACTIVE, models.Campaign.States.DRAFT
        )
    ]

    @action(detail=True, methods=['get'])
    def orders(self, request, uuid=None):
        campaign = self.get_object()
        resources = models.DiscountedResource.objects.filter(
            campaign=campaign
        ).values_list('resource', flat=True)
        orders = marketplace_models.Order.objects.filter(resource__in=resources)
        serializer = marketplace_serializers.OrderDetailsSerializer(
            instance=orders, many=True, context={'view': self, 'request': request}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'])
    def resources(self, request, uuid=None):
        campaign = self.get_object()
        discounted_resources = models.DiscountedResource.objects.filter(
            campaign=campaign
        ).values_list('resource', flat=True)
        resources = marketplace_models.Resource.objects.filter(
            id__in=discounted_resources
        )
        serializer = marketplace_serializers.ResourceSerializer(
            resources, many=True, context={'view': self, 'request': request}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)
