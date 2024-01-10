import copy

from rest_framework import serializers

from waldur_core.core import signals as core_signals
from waldur_core.structure.managers import get_connected_customers
from waldur_mastermind.booking import models as booking_models
from waldur_mastermind.google import serializers as google_serializers
from waldur_mastermind.marketplace import serializers as marketplace_serializers

from . import PLUGIN_NAME


class BookingSlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = booking_models.BookingSlot
        fields = ("start", "end", "backend_id")


class BookingResourceSerializer(marketplace_serializers.ResourceSerializer):
    created_by = serializers.HyperlinkedRelatedField(
        view_name="user-detail",
        source="creation_order.created_by",
        lookup_field="uuid",
        read_only=True,
    )
    created_by_username = serializers.ReadOnlyField(
        source="creation_order.created_by.username"
    )
    created_by_full_name = serializers.ReadOnlyField(
        source="creation_order.created_by.full_name"
    )
    consumer_reviewed_by = serializers.HyperlinkedRelatedField(
        view_name="user-detail",
        source="creation_order.consumer_reviewed_by",
        lookup_field="uuid",
        read_only=True,
    )
    consumer_reviewed_by_username = serializers.ReadOnlyField(
        source="creation_order.consumer_reviewed_by.username"
    )
    consumer_reviewed_by_full_name = serializers.ReadOnlyField(
        source="creation_order.consumer_reviewed_by.full_name"
    )
    description = serializers.SerializerMethodField()
    slots = serializers.SerializerMethodField()

    class Meta(marketplace_serializers.ResourceSerializer.Meta):
        view_name = "booking-resource-detail"
        fields = marketplace_serializers.ResourceSerializer.Meta.fields + (
            "url",
            "created_by",
            "created_by_username",
            "created_by_full_name",
            "consumer_reviewed_by",
            "consumer_reviewed_by_username",
            "consumer_reviewed_by_full_name",
            "description",
            "slots",
        )
        extra_kwargs = copy.copy(
            marketplace_serializers.ResourceSerializer.Meta.extra_kwargs
        )
        extra_kwargs["url"] = {"lookup_field": "uuid", "read_only": True}

    def get_description(self, resource):
        return resource.attributes.get("description", "")

    def get_slots(self, resource):
        slots = booking_models.BookingSlot.objects.filter(resource=resource).order_by(
            "start"
        )
        return BookingSlotSerializer(instance=slots, many=True).data


class BookingSerializer(serializers.Serializer):
    created_by_full_name = serializers.SerializerMethodField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()

    def get_created_by_full_name(self, booking):
        order = booking.order

        if not order:
            return "google calendar"

        user_customers = get_connected_customers(self.context["request"].user)
        creator_customers = get_connected_customers(order.created_by)

        if user_customers.intersection(creator_customers):
            return order.created_by.full_name


class OfferingSerializer(marketplace_serializers.PublicOfferingDetailsSerializer):
    googlecalendar = google_serializers.GoogleCalendarSerializer(required=False)

    class Meta(marketplace_serializers.PublicOfferingDetailsSerializer.Meta):
        fields = marketplace_serializers.PublicOfferingDetailsSerializer.Meta.fields + (
            "googlecalendar",
        )
        view_name = "booking-offering-detail"


def get_google_calendar_public(serializer, offering):
    if offering.type != PLUGIN_NAME or not hasattr(offering, "googlecalendar"):
        return

    return offering.googlecalendar.public


def add_google_calendar_info(sender, fields, **kwargs):
    fields["google_calendar_is_public"] = serializers.SerializerMethodField()
    setattr(sender, "get_google_calendar_is_public", get_google_calendar_public)


core_signals.pre_serializer_fields.connect(
    add_google_calendar_info,
    sender=marketplace_serializers.ProviderOfferingDetailsSerializer,
)

core_signals.pre_serializer_fields.connect(
    add_google_calendar_info,
    sender=marketplace_serializers.PublicOfferingDetailsSerializer,
)


def get_google_calendar_link(serializer, offering):
    try:
        return offering.googlecalendar.http_link
    except AttributeError:
        return


def add_google_calendar_link(sender, fields, **kwargs):
    fields["google_calendar_link"] = serializers.SerializerMethodField()
    setattr(sender, "get_google_calendar_link", get_google_calendar_link)


core_signals.pre_serializer_fields.connect(
    sender=marketplace_serializers.ProviderOfferingDetailsSerializer,
    receiver=add_google_calendar_link,
)

core_signals.pre_serializer_fields.connect(
    sender=marketplace_serializers.PublicOfferingDetailsSerializer,
    receiver=add_google_calendar_link,
)
