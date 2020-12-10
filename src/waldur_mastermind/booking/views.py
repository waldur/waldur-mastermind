from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_mastermind.booking.utils import get_offering_bookings
from waldur_mastermind.marketplace import filters as marketplace_filters
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace.callbacks import (
    resource_creation_canceled,
    resource_creation_succeeded,
)

from . import PLUGIN_NAME, filters, serializers, tasks


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
            order_item = resource_creation_canceled(resource, validate=True)

        return Response(
            {'order_item_uuid': order_item.uuid.hex}, status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'])
    def accept(self, request, uuid=None):
        resource = self.get_object()

        with transaction.atomic():
            order_item = resource_creation_succeeded(resource, validate=True)

        return Response(
            {'order_item_uuid': order_item.uuid.hex}, status=status.HTTP_200_OK
        )

    reject_validators = accept_validators = [
        core_validators.StateValidator(models.Resource.States.CREATING)
    ]


class OfferingBookingsViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Offering.objects.filter(type=PLUGIN_NAME)
    filter_backends = (
        DjangoFilterBackend,
        filters.CustomersFilterBackend,
    )
    lookup_field = 'uuid'
    serializer_class = marketplace_serializers.OfferingDetailsSerializer

    def retrieve(self, request, uuid=None):
        offerings = models.Offering.objects.all().filter_for_user(request.user)
        offering = get_object_or_404(offerings, uuid=uuid)
        bookings = get_offering_bookings(offering)
        serializer = serializers.BookingSerializer(instance=bookings, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def google_calendar_sync(self, request, uuid=None):
        offering = self.get_object()
        tasks.sync_bookings_to_google_calendar.delay(offering.uuid.hex)
        return Response('OK', status=status.HTTP_200_OK)

    def google_credential_exists(offering):
        service_provider = getattr(offering.customer, 'serviceprovider', None)
        credentials = getattr(service_provider, 'googlecredentials', None)
        if (
            not credentials
            or not credentials.calendar_token
            or not credentials.calendar_refresh_token
        ):
            raise ValidationError(_('Google credentials do not exist.'))

    google_calendar_sync_validators = [google_credential_exists]
