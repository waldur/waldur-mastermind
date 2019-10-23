from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework import serializers as rf_serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_mastermind.marketplace import filters as marketplace_filters
from waldur_mastermind.marketplace import models

from . import PLUGIN_NAME, filters, serializers
from .log import event_logger


class ResourceViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Resource.objects.filter(offering__type=PLUGIN_NAME)
    filter_backends = (
        DjangoFilterBackend,
        filters.OfferingCustomersFilterBackend,
    )
    filterset_class = marketplace_filters.ResourceFilter
    lookup_field = 'uuid'
    serializer_class = serializers.BookingResourceSerializer

    @action(detail=True, methods=['post'])
    def reject(self, request, uuid=None):
        resource = self.get_object()

        with transaction.atomic():
            try:
                order_item = models.OrderItem.objects.get(
                    resource=resource,
                    offering=resource.offering,
                    type=models.OrderItem.Types.CREATE,
                    state=models.OrderItem.States.EXECUTING,
                )
            except models.OrderItem.DoesNotExist:
                raise rf_serializers.ValidationError(_('Resource rejecting is not available because '
                                                       'the reference order item is not found.'))
            except models.OrderItem.MultipleObjectsReturned:
                raise rf_serializers.ValidationError(_('Resource rejecting is not available because '
                                                       'several reference order items are found.'))
            order_item.set_state_terminated()
            order_item.save()
            resource.set_state_terminated()
            resource.save()

        return Response({'order_item_uuid': order_item.uuid.hex}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def accept(self, request, uuid=None):
        resource = self.get_object()

        with transaction.atomic():
            try:
                order_item = models.OrderItem.objects.get(
                    resource=resource,
                    offering=resource.offering,
                    type=models.OrderItem.Types.CREATE,
                    state=models.OrderItem.States.EXECUTING,
                )
            except models.OrderItem.DoesNotExist:
                raise rf_serializers.ValidationError(_('Resource accepting is not available because '
                                                       'the reference order item is not found.'))
            except models.OrderItem.MultipleObjectsReturned:
                raise rf_serializers.ValidationError(_('Resource accepting is not available because '
                                                       'several reference order items are found.'))
            order_item.set_state_done()
            order_item.save()
            resource.set_state_ok()
            resource.save()

            event_logger.waldur_booking.info(
                'Device booking {resource_name} has been accepted.',
                event_type='device_booking_is_accepted',
                event_context={
                    'resource': resource,
                })

        return Response({'order_item_uuid': order_item.uuid.hex}, status=status.HTTP_200_OK)

    reject_validators = accept_validators = [
        core_validators.StateValidator(models.Resource.States.CREATING)
    ]
