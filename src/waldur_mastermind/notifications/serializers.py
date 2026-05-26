from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core.serializers import (
    AugmentedSerializerMixin,
    RestrictedSerializerMixin,
)
from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace.models import Offering

from . import models, utils


class QuerySerializer(serializers.Serializer):
    customers = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=Customer.objects.all(),
        many=True,
        required=False,
    )
    offerings = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=Offering.objects.all(),
        many=True,
        required=False,
    )
    all_users = serializers.BooleanField(default=False)


def format_options(options):
    return [{"name": option.name, "uuid": option.uuid.hex} for option in options]


def serialize_query(query):
    serialized_query = {}
    if "customers" in query:
        serialized_query["customers"] = format_options(query["customers"])
    if "offerings" in query:
        serialized_query["offerings"] = format_options(query["offerings"])
    serialized_query["all_users"] = query.get("all_users", False)
    return serialized_query


class BroadcastMessageSerializer(
    RestrictedSerializerMixin, serializers.ModelSerializer
):
    author_full_name = serializers.ReadOnlyField(source="author.full_name")
    state = serializers.ReadOnlyField()
    emails = serializers.ReadOnlyField()

    class Meta:
        model = models.BroadcastMessage
        fields = (
            "uuid",
            "created",
            "subject",
            "body",
            "query",
            "author_full_name",
            "emails",
            "state",
            "send_at",
        )

    def validate_query(self, query):
        serializer = QuerySerializer(data=query)
        serializer.is_valid()
        return serializer.validated_data

    def create(self, validated_data):
        current_user = self.context["request"].user
        validated_data["author"] = current_user
        validated_data["emails"] = utils.get_user_emails_for_query(
            validated_data["query"]
        )
        validated_data["query"] = serialize_query(validated_data["query"])
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data["emails"] = utils.get_user_emails_for_query(
            validated_data["query"]
        )
        validated_data["query"] = serialize_query(validated_data["query"])
        return super().update(instance, validated_data)


class MessageTemplateSerializer(
    serializers.HyperlinkedModelSerializer,
):
    subject = core_serializers.HTMLCleanField()
    body = core_serializers.HTMLCleanField()

    class Meta:
        model = models.MessageTemplate
        fields = (
            "url",
            "uuid",
            "name",
            "subject",
            "body",
        )
        extra_kwargs = {"url": {"lookup_field": "uuid"}}


class AdminAnnouncementSerializer(
    AugmentedSerializerMixin, RestrictedSerializerMixin, serializers.ModelSerializer
):
    class Meta:
        model = models.AdminAnnouncement
        fields = (
            "uuid",
            "description",
            "active_from",
            "active_to",
            "is_active",
            "type",
            "created",
        )


class NotificationRecipientOfferingSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()


class NotificationRecipientCustomerSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()


class NotificationRecipientSerializer(serializers.Serializer):
    full_name = serializers.CharField(required=False, allow_null=True)
    email = serializers.EmailField()
    offerings = NotificationRecipientOfferingSerializer(many=True)
    customers = NotificationRecipientCustomerSerializer(many=True)
