from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, views
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_mastermind.booking.utils import get_offering_bookings
from waldur_mastermind.google import models as google_models
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace import permissions as marketplace_permissions
from waldur_mastermind.marketplace.callbacks import (
    resource_creation_canceled,
    resource_creation_succeeded,
)

from . import PLUGIN_NAME, executors, filters, serializers


class ResourceViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Resource.objects.filter(offering__type=PLUGIN_NAME)
    filter_backends = (
        DjangoFilterBackend,
        filters.ResourceOwnerOrCreatorFilterBackend,
    )
    filterset_class = filters.BookingResourceFilter
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

    accept_permissions = [marketplace_permissions.user_is_owner_or_service_manager]


class OfferingViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Offering.objects.filter(type=PLUGIN_NAME)
    filter_backends = (
        DjangoFilterBackend,
        filters.CustomersFilterBackend,
    )
    lookup_field = 'uuid'
    serializer_class = serializers.OfferingSerializer

    @action(detail=True, methods=['post'])
    def google_calendar_sync(self, request, uuid=None):
        offering = self.get_object()
        self._get_or_create_google_calendar(offering)
        transaction.on_commit(
            lambda: executors.GoogleCalendarSyncExecutor.execute(
                offering.googlecalendar, updated_fields=None
            )
        )
        return Response('OK', status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'])
    def share_google_calendar(self, request, uuid=None):
        offering = self.get_object()
        self._get_or_create_google_calendar(offering)
        transaction.on_commit(
            lambda: executors.GoogleCalendarShareExecutor.execute(
                offering.googlecalendar, updated_fields=['public']
            )
        )
        return Response('OK', status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'])
    def unshare_google_calendar(self, request, uuid=None):
        offering = self.get_object()
        self._get_or_create_google_calendar(offering)
        transaction.on_commit(
            lambda: executors.GoogleCalendarUnShareExecutor.execute(
                offering.googlecalendar, updated_fields=['public']
            )
        )
        return Response('OK', status=status.HTTP_202_ACCEPTED)

    def validate_google_credential(offering):
        service_provider = getattr(offering.customer, 'serviceprovider', None)
        credentials = getattr(service_provider, 'googlecredentials', None)
        if (
            not credentials
            or not credentials.calendar_token
            or not credentials.calendar_refresh_token
        ):
            raise ValidationError(_('Google credentials do not exist.'))

    def validate_google_calendar_state(offering):
        try:
            google_calendar = google_models.GoogleCalendar.objects.get(
                offering=offering
            )

            if google_calendar.state not in (
                google_models.GoogleCalendar.States.OK,
                google_models.GoogleCalendar.States.ERRED,
            ):
                raise ValidationError(_('The calendar cannot be updated.'))
        except google_models.GoogleCalendar.DoesNotExist:
            # a calendar will be created in waldur later
            pass

    def validate_sharing_available(offering):
        try:
            google_calendar = google_models.GoogleCalendar.objects.get(
                offering=offering
            )

            if google_calendar.public:
                raise ValidationError(_('The calendar is public already.'))
        except google_models.GoogleCalendar.DoesNotExist:
            # a calendar will be created in waldur later
            pass

    def validate_unsharing_available(offering):
        try:
            google_calendar = google_models.GoogleCalendar.objects.get(
                offering=offering
            )

            if not google_calendar.public:
                raise ValidationError(_('The calendar is private already.'))
        except google_models.GoogleCalendar.DoesNotExist:
            raise ValidationError(_('The calendar does not exist.'))

    google_calendar_sync_validators = [
        validate_google_credential,
        validate_google_calendar_state,
    ]
    share_google_calendar_validators = [
        validate_google_credential,
        validate_google_calendar_state,
        validate_sharing_available,
    ]
    unshare_google_calendar_validators = [
        validate_google_credential,
        validate_google_calendar_state,
        validate_unsharing_available,
    ]

    def _get_or_create_google_calendar(self, offering):
        google_calendar, _ = google_models.GoogleCalendar.objects.get_or_create(
            offering=offering
        )
        return google_calendar


class OfferingBookingsViewSet(views.APIView):
    def get(self, request, uuid):
        offerings = models.Offering.objects.all().filter_for_user(request.user)
        offering = get_object_or_404(offerings, uuid=uuid)
        bookings = get_offering_bookings(offering)
        serializer = serializers.BookingSerializer(
            instance=bookings, many=True, context={'request': request}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)
