from __future__ import unicode_literals

from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import detail_route
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import utils as structure_utils
from waldur_mastermind.marketplace import serializers, models

from . import PLUGIN_NAME, filters


class ResourceViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Resource.objects.exclude(offering__type=PLUGIN_NAME)
    filter_backends = (
        DjangoFilterBackend,
        filters.OfferingCustomersFilterBackend
    )
    lookup_field = 'uuid'
    serializer_class = serializers.ResourceSerializer

    @detail_route(methods=['post'])
    def reject(self, request, uuid=None):
        resource = self.get_object()

        with transaction.atomic():
            order_item = models.OrderItem(
                resource=resource,
                offering=resource.offering,
            )
            order_item.set_state_terminated()
            order_item.save()
            resource.set_state_terminated()
            resource.save()

        return Response({'order_item_uuid': order_item.uuid}, status=status.HTTP_200_OK)

    @detail_route(methods=['post'])
    def accept(self, request, uuid=None):
        resource = self.get_object()

        with transaction.atomic():
            order_item = models.OrderItem(
                resource=resource,
                offering=resource.offering,
            )
            order_item.set_state_done()
            order_item.save()
            resource.set_state_done()
            resource.save()

        return Response({'order_item_uuid': order_item.uuid}, status=status.HTTP_200_OK)

    accept_permissions = reject_permissions = [structure_permissions.is_administrator]
    reject_validators = accept_validators = [
        core_validators.StateValidator(models.Resource.States.CREATING),
        structure_utils.check_customer_blocked
    ]
