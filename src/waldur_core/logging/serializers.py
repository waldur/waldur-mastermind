import logging
import uuid

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core.fields import NaturalChoiceField
from waldur_core.core.serializers import RestrictedSerializerMixin
from waldur_core.logging import backend, loggers, models, utils

logger = logging.getLogger(__name__)


class EventSerializer(RestrictedSerializerMixin, serializers.ModelSerializer):
    context = serializers.JSONField(read_only=True)

    class Meta:
        model = models.Event
        fields = ("uuid", "created", "event_type", "message", "context")


class BaseHookSerializer(serializers.HyperlinkedModelSerializer):
    author_uuid = serializers.ReadOnlyField(source="user.uuid")
    author_fullname = serializers.ReadOnlyField(source="user.full_name")
    author_username = serializers.ReadOnlyField(source="user.username")
    author_email = serializers.ReadOnlyField(source="user.email")
    hook_type = serializers.SerializerMethodField()

    class Meta:
        model = models.BaseHook

        fields = (
            "url",
            "uuid",
            "is_active",
            "author_uuid",
            "event_types",
            "event_groups",
            "created",
            "modified",
            "hook_type",
            "author_fullname",
            "author_username",
            "author_email",
        )

        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }

    def get_fields(self):
        """
        When static declaration is used, event type choices are fetched too early -
        even before all apps are initialized. As a result, some event types are missing.
        When dynamic declaration is used, all valid event types are available as choices.
        """
        fields = super().get_fields()
        fields["event_types"] = serializers.MultipleChoiceField(
            choices=loggers.get_valid_events(), required=False
        )
        fields["event_groups"] = serializers.MultipleChoiceField(
            choices=loggers.get_event_groups_keys(), required=False
        )
        return fields

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)

    def validate(self, attrs):
        if (
            not self.instance
            and "event_types" not in attrs
            and "event_groups" not in attrs
        ):
            raise serializers.ValidationError(
                _("Please specify list of event_types or event_groups.")
            )

        if "event_groups" in attrs:
            events = list(attrs.get("event_types", []))
            groups = list(attrs.get("event_groups", []))
            events = sorted(set(loggers.expand_event_groups(groups)) | set(events))

            attrs["event_types"] = events
            attrs["event_groups"] = groups

        elif "event_types" in attrs:
            attrs["event_types"] = list(attrs["event_types"])

        return attrs

    def get_hook_type(self, hook):
        raise NotImplementedError


class SummaryHookSerializer(serializers.Serializer):
    def to_representation(self, instance):
        serializer = self.get_hook_serializer(instance.__class__)
        return serializer(instance, context=self.context).data

    def get_hook_serializer(self, cls):
        for serializer in BaseHookSerializer.__subclasses__():
            if serializer.Meta.model == cls:
                return serializer
        raise ValueError("Hook serializer for %s class is not found" % cls)


class WebHookSerializer(BaseHookSerializer):
    content_type = NaturalChoiceField(
        models.WebHook.ContentTypeChoices.CHOICES, required=False
    )

    class Meta(BaseHookSerializer.Meta):
        model = models.WebHook
        fields = BaseHookSerializer.Meta.fields + ("destination_url", "content_type")

    def get_hook_type(self, hook):
        return "webhook"


class EmailHookSerializer(BaseHookSerializer):
    class Meta(BaseHookSerializer.Meta):
        model = models.EmailHook
        fields = BaseHookSerializer.Meta.fields + ("email",)

    def get_hook_type(self, hook):
        return "email"


class EventSubscriptionSerializer(serializers.HyperlinkedModelSerializer):
    observable_objects = serializers.JSONField(default=list)
    user_uuid = serializers.ReadOnlyField(source="user.uuid")
    user_username = serializers.ReadOnlyField(source="user.username")
    user_full_name = serializers.ReadOnlyField(source="user.full_name")

    class Meta:
        model = models.EventSubscription
        fields = (
            "uuid",
            "url",
            "description",
            "user",
            "user_uuid",
            "user_username",
            "user_full_name",
            "observable_objects",
            "created",
            "modified",
            "source_ip",
        )
        read_only_fields = ("user", "source_ip")
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "event-subscription-detail",
            },
            "user": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
            "description": {"required": False},
        }

    def validate_observable_objects(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError(
                "The observable_objects field must be a list of JSON objects."
            )

        required_keys = {"object_type", "object_id"}
        for item in value:
            request_keys = set(item.keys())
            if not request_keys.issubset(required_keys):
                raise serializers.ValidationError(
                    f"The observable_objects field must contain keys only: {', '.join(required_keys)}"
                )

            if not isinstance(item.get("object_type"), str):
                raise serializers.ValidationError("object_type value must be a string.")

            object_types = [member.value for member in utils.ObservableObjectType]

            if item.get("object_type") not in object_types:
                raise serializers.ValidationError(
                    f"Invalid object_type. Must be one of: {', '.join(object_types)}"
                )

            if item.get("object_id") and not isinstance(item.get("object_id"), int):
                raise serializers.ValidationError("object_id value must be an integer.")

        return value

    @transaction.atomic
    def create(self, validated_data):
        user = validated_data["user"]
        object_uuid = uuid.uuid4().hex
        validated_data["uuid"] = object_uuid
        vhost_name = f"{user.uuid.hex}"
        rmq_backend = backend.RabbitMQManagementBackend()

        # Create virtual host
        if not rmq_backend.create_rabbitmq_virtual_host(vhost_name):
            logger.error("Failed to create RabbitMQ virtual host: %s", vhost_name)
            raise serializers.ValidationError("Failed to create RabbitMQ virtual host")

        # Create RabbitMQ user
        if not rmq_backend.create_rabbitmq_user(object_uuid, user.auth_token.key):
            logger.error("Failed to create RabbitMQ user: %s", object_uuid)
            raise serializers.ValidationError("Failed to create RabbitMQ user")

        # Assign permissions: consumer needs `configure` perm to create a queue in RMQ
        permissions = {"configure": ".*", "write": ".*", "read": ".*"}
        if not rmq_backend.assign_rabbitmq_vhost_permissions(
            object_uuid, vhost_name, permissions
        ):
            logger.error(
                "Failed to assign RabbitMQ permissions for user: %s", object_uuid
            )
            # Cleanup user if permission assignment fails
            rmq_backend.delete_rabbitmq_user(object_uuid)
            raise serializers.ValidationError("Failed to assign RabbitMQ permissions")

        return super().create(validated_data)
