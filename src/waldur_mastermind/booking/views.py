from django.db import transaction
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import status, views
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory
from waldur_mastermind.booking.utils import get_offering_bookings_and_busy_slots
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.callbacks import (
    resource_creation_canceled,
    resource_creation_succeeded,
)
from waldur_mastermind.marketplace.enums import BOOKING_OFFERING, ResourceStates
from waldur_mastermind.proposal import managers as proposal_managers

from . import filters, permissions, serializers


class ResourceViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Resource.objects.filter(offering__type=BOOKING_OFFERING).order_by(
        "name"
    )
    filter_backends = (
        DjangoFilterBackend,
        filters.ResourceOwnerOrCreatorFilterBackend,
    )
    filterset_class = filters.BookingResourceFilter
    lookup_field = "uuid"
    serializer_class = serializers.BookingResourceSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.BookingOrderUUIDSerializer},
        request=None,
    )
    @action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        resource: models.Resource = self.get_object()

        with transaction.atomic():
            order = resource_creation_canceled(resource, validate=True)

        return Response({"order_uuid": order.uuid.hex}, status=status.HTTP_200_OK)

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.BookingOrderUUIDSerializer},
        request=None,
    )
    @action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        resource: models.Resource = self.get_object()

        with transaction.atomic():
            order = resource_creation_succeeded(resource, validate=True)

        return Response({"order_uuid": order.uuid.hex}, status=status.HTTP_200_OK)

    reject_validators = accept_validators = [
        core_validators.StateValidator(ResourceStates.CREATING)
    ]

    accept_permissions = [
        permission_factory(
            PermissionEnum.ACCEPT_BOOKING_REQUEST,
            ["project.customer", "offering", "offering.customer"],
        )
    ]

    reject_permissions = [permissions.user_can_reject_order]


class OfferingViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Offering.objects.filter(type=BOOKING_OFFERING).order_by("name")
    filter_backends = (
        DjangoFilterBackend,
        filters.CustomersFilterBackend,
    )
    lookup_field = "uuid"
    serializer_class = serializers.OfferingSerializer

    def get_queryset(self):
        return proposal_managers.annotate_offerings_open_for_proposals(
            super().get_queryset()
        )


class OfferingBookingsViewSet(views.APIView):
    @extend_schema(request=None, responses=serializers.BookingSerializer(many=True))
    def get(self, request, uuid):
        offerings = (
            models.Offering.objects.all().filter_by_ordering_availability_for_user(
                request.user
            )
        )
        offering = get_object_or_404(offerings, uuid=uuid)
        bookings = get_offering_bookings_and_busy_slots(offering)
        serializer = serializers.BookingSerializer(
            instance=bookings, many=True, context={"request": request}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)
