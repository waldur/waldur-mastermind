from django.utils.translation import ugettext_lazy as _
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.validators import ValidationError

from waldur_core.core.views import ActionsViewSet
from waldur_core.structure import models as structure_models
from waldur_core.structure import views as structure_views
from waldur_core.structure.serializers import ServiceSettingsSerializer
from waldur_mastermind.marketplace.serializers import OfferingDetailsSerializer

from . import filters, models, serializers
from .backend import WaldurBackend


class RemoteWaldurServiceViewSet(structure_views.BaseServiceViewSet):
    queryset = models.RemoteWaldurService.objects.all()
    serializer_class = serializers.ServiceSerializer


class ServiceProjectLinkViewSet(structure_views.BaseServiceProjectLinkViewSet):
    queryset = models.RemoteWaldurServiceProjectLink.objects.all()
    serializer_class = serializers.ServiceProjectLinkSerializer
    filterset_class = filters.ServiceProjectLinkFilter


class RemoteWaldurViewSet(ActionsViewSet):
    queryset = None
    disabled_actions = ['retrieve', 'update', 'delete', 'list', 'create']
    serializer_class = OfferingDetailsSerializer

    @action(detail=False, methods=['post'])
    def remote_customers(self, request):
        serializer: ServiceSettingsSerializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service_settings_json = serializer.validated_data

        waldur_backend = WaldurBackend(
            structure_models.ServiceSettings(**service_settings_json)
        )
        customers_json = waldur_backend.get_remote_customers()
        return Response(customers_json, status=status.HTTP_200_OK)

    remote_customers_serializer_class = ServiceSettingsSerializer

    @action(detail=False, methods=['post'])
    def shared_offerings(self, request):
        if 'customer_uuid' not in request.query_params:
            raise ValidationError(
                {'url': _('customer_uuid field must be present in query parameters')}
            )

        remote_customer_uuid = request.query_params['customer_uuid']

        serializer: ServiceSettingsSerializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service_settings_json = serializer.validated_data

        waldur_backend = WaldurBackend(
            structure_models.ServiceSettings(**service_settings_json)
        )
        offerings_json = waldur_backend.get_importable_offerings(remote_customer_uuid)
        return Response(offerings_json, status=status.HTTP_200_OK)

    shared_offerings_serializer_class = ServiceSettingsSerializer
