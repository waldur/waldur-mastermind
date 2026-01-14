import logging
import uuid

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core.fields import NaturalChoiceField
from waldur_core.core.serializers import RestrictedSerializerMixin
from waldur_core.logging import backend, event_logger, models, utils

logger = logging.getLogger(__name__)


class EventSerializer(RestrictedSerializerMixin, serializers.ModelSerializer):
    context = serializers.JSONField(read_only=True)

    class Meta:
        model = models.Event
        fields = ("uuid", "created", "event_type", "message", "context")


class BaseHookSerializer(serializers.HyperlinkedModelSerializer):
    author_uuid = serializers.UUIDField(read_only=True, source="user.uuid")
    author_fullname = serializers.CharField(read_only=True, source="user.full_name")
    author_username = serializers.CharField(read_only=True, source="user.username")
    author_email = serializers.CharField(read_only=True, source="user.email")
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
            choices=event_logger.get_valid_events(), required=False
        )
        fields["event_groups"] = serializers.MultipleChoiceField(
            choices=event_logger.get_event_groups_keys(), required=False
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
            events = sorted(set(event_logger.expand_event_groups(groups)) | set(events))

            attrs["event_types"] = events
            attrs["event_groups"] = groups

        elif "event_types" in attrs:
            attrs["event_types"] = list(attrs["event_types"])

        return attrs

    def get_hook_type(self, hook) -> str:
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

    def get_hook_type(self, hook) -> str:
        return "webhook"


class EmailHookSerializer(BaseHookSerializer):
    class Meta(BaseHookSerializer.Meta):
        model = models.EmailHook
        fields = BaseHookSerializer.Meta.fields + ("email",)

    def get_hook_type(self, hook) -> str:
        return "email"


class EventSubscriptionSerializer(serializers.HyperlinkedModelSerializer):
    observable_objects = serializers.JSONField(
        default=list,
        help_text="List of objects to observe. Each item must have 'object_type' "
        "(one of: order, user_role, resource, offering_user, importable_resources, "
        "service_account, course_account, resource_periodic_limits) "
        "and optionally 'object_id' (integer). "
        'Example: [{"object_type": "resource"}, {"object_type": "order", "object_id": 123}]',
    )
    user_uuid = serializers.UUIDField(read_only=True, source="user.uuid")
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
        vhost_name = user.uuid.hex
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


class EventStatsSerializer(serializers.Serializer):
    year = serializers.IntegerField(read_only=True)
    month = serializers.IntegerField(read_only=True)
    count = serializers.IntegerField(read_only=True)


class RmqConnectionSerializer(serializers.Serializer):
    source_ip = serializers.IPAddressField(read_only=True)
    vhost = serializers.CharField(read_only=True)


class RmqUserStatsItemSerializer(serializers.Serializer):
    username = serializers.CharField(read_only=True)
    connections = RmqConnectionSerializer(many=True, read_only=True)


class RmqUserStatsSerializer(serializers.ListSerializer):
    child = RmqUserStatsItemSerializer()


class RmqWaldurUserSerializer(serializers.Serializer):
    full_name = serializers.CharField(read_only=True)
    username = serializers.CharField(read_only=True)
    email = serializers.EmailField(read_only=True)


class RmqSubscriptionSerializer(serializers.Serializer):
    created = serializers.DateTimeField(read_only=True)
    uuid = serializers.UUIDField(read_only=True)
    source_ip = serializers.IPAddressField(read_only=True)


class RmqVHostStatsItemSerializer(serializers.Serializer):
    name = serializers.CharField(read_only=True)
    waldur_user = RmqWaldurUserSerializer(read_only=True)
    subscriptions = RmqSubscriptionSerializer(many=True, read_only=True)


class RmqVHostStatsSerializer(serializers.ListSerializer):
    child = RmqVHostStatsItemSerializer()


class EmailLogSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.EmailLog
        fields = (
            "uuid",
            "url",
            "sent_at",
            "subject",
            "body",
            "emails",
        )

        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "email-log-detail",
            },
        }


# RabbitMQ Stats API Serializers


class RmqQueueStatsSerializer(serializers.Serializer):
    """Serializer for individual RabbitMQ queue statistics."""

    name = serializers.CharField(
        read_only=True,
        help_text="Queue name (e.g., 'subscription_{uuid}_offering_{uuid}_{type}')",
    )
    messages = serializers.IntegerField(
        read_only=True,
        help_text="Total number of messages in the queue",
    )
    messages_ready = serializers.IntegerField(
        read_only=True,
        help_text="Number of messages ready for delivery",
    )
    messages_unacknowledged = serializers.IntegerField(
        read_only=True,
        help_text="Number of messages awaiting acknowledgement",
    )
    consumers = serializers.IntegerField(
        read_only=True,
        help_text="Number of active consumers for this queue",
    )
    subscription_uuid = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Parsed subscription UUID from queue name",
    )
    offering_uuid = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Parsed offering UUID from queue name",
    )
    object_type = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Parsed object type from queue name (e.g., 'resource', 'order')",
    )
    message_ttl = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Message TTL in milliseconds",
    )
    max_length = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Maximum number of messages in queue",
    )
    max_length_bytes = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Maximum total size of messages in bytes",
    )
    expires = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Queue TTL - auto-delete after idle in milliseconds",
    )
    overflow = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Behavior when full: 'drop-head', 'reject-publish', or 'reject-publish-dlx'",
    )
    dead_letter_exchange = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Dead letter exchange name",
    )
    dead_letter_routing_key = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Dead letter routing key",
    )
    max_priority = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Maximum priority level (1-255)",
    )
    queue_mode = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Queue mode: 'default' or 'lazy'",
    )
    queue_type = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Queue type: 'classic', 'quorum', or 'stream'",
    )


class RmqStatsUserSerializer(serializers.Serializer):
    """Serializer for Waldur user information linked to a RabbitMQ vhost."""

    uuid = serializers.CharField(
        read_only=True,
        help_text="Waldur user UUID",
    )
    username = serializers.CharField(
        read_only=True,
        help_text="Waldur username",
    )
    full_name = serializers.CharField(
        read_only=True,
        help_text="User's full name",
    )


class RmqVhostStatsSerializer(serializers.Serializer):
    """Serializer for RabbitMQ vhost statistics with queues."""

    name = serializers.CharField(
        read_only=True,
        help_text="Virtual host name (corresponds to Waldur user UUID)",
    )
    user = RmqStatsUserSerializer(
        read_only=True,
        allow_null=True,
        help_text="Waldur user associated with this vhost",
    )
    queues = RmqQueueStatsSerializer(
        many=True,
        read_only=True,
        help_text="List of subscription queues in this vhost",
    )
    total_messages = serializers.IntegerField(
        read_only=True,
        help_text="Total messages across all queues in this vhost",
    )


class RmqStatsResponseSerializer(serializers.Serializer):
    """
    Response serializer for RabbitMQ subscription queue statistics.

    Provides aggregated statistics across all vhosts with subscription queues,
    including Waldur user information and parsed queue name components.
    """

    vhosts = RmqVhostStatsSerializer(
        many=True,
        read_only=True,
        help_text="List of vhosts with their subscription queues",
    )
    total_messages = serializers.IntegerField(
        read_only=True,
        help_text="Total messages across all subscription queues",
    )
    total_queues = serializers.IntegerField(
        read_only=True,
        help_text="Total number of subscription queues",
    )


class RmqPurgeRequestSerializer(serializers.Serializer):
    """Request serializer for purging or deleting RabbitMQ queues."""

    vhost = serializers.CharField(
        required=False,
        help_text="Virtual host name containing the queue(s)",
    )
    queue_name = serializers.CharField(
        required=False,
        help_text="Specific queue name (requires vhost)",
    )
    queue_pattern = serializers.CharField(
        required=False,
        help_text="Glob pattern to match queue names (e.g., '*_resource'). Requires vhost.",
    )
    purge_all_subscription_queues = serializers.BooleanField(
        required=False,
        default=False,
        help_text="If true, purge all subscription queues across all vhosts",
    )
    delete_queue = serializers.BooleanField(
        required=False,
        default=False,
        help_text="If true, delete the queue(s) entirely instead of just purging messages",
    )
    delete_all_subscription_queues = serializers.BooleanField(
        required=False,
        default=False,
        help_text="If true, delete all subscription queues across all vhosts",
    )

    def validate(self, attrs):
        vhost = attrs.get("vhost")
        queue_name = attrs.get("queue_name")
        queue_pattern = attrs.get("queue_pattern")
        purge_all = attrs.get("purge_all_subscription_queues", False)
        delete_all = attrs.get("delete_all_subscription_queues", False)

        if not purge_all and not delete_all and not vhost:
            raise serializers.ValidationError(
                "Must specify 'purge_all_subscription_queues', 'delete_all_subscription_queues', "
                "or 'vhost' with 'queue_name'/'queue_pattern'"
            )

        if vhost and not queue_name and not queue_pattern:
            raise serializers.ValidationError(
                "When 'vhost' is specified, must also provide 'queue_name' or 'queue_pattern'"
            )

        return attrs


class RmqPurgeResponseSerializer(serializers.Serializer):
    """Response serializer for queue purge/delete operations."""

    purged_queues = serializers.IntegerField(
        read_only=True,
        help_text="Number of queues that were purged",
    )
    purged_messages = serializers.IntegerField(
        read_only=True,
        help_text="Total number of messages that were purged",
    )
    deleted_queues = serializers.IntegerField(
        read_only=True,
        help_text="Number of queues that were deleted",
    )


class RmqStatsErrorSerializer(serializers.Serializer):
    """Error response serializer for RabbitMQ stats operations."""

    error = serializers.CharField(
        read_only=True,
        help_text="Error message describing what went wrong",
    )


# Enriched Connection Serializers (Part A)


class RmqClientPropertiesSerializer(serializers.Serializer):
    """Serializer for RabbitMQ client properties from connection."""

    product = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Client product name (e.g., 'pika', 'amqp-client')",
    )
    version = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Client library version",
    )
    platform = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Client platform (e.g., 'Python 3.11')",
    )


class RmqEnrichedConnectionSerializer(serializers.Serializer):
    """Serializer for enriched RabbitMQ connection data with traffic stats."""

    source_ip = serializers.CharField(
        read_only=True,
        help_text="Client IP address",
    )
    vhost = serializers.CharField(
        read_only=True,
        help_text="Virtual host name",
    )
    connected_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Connection establishment timestamp",
    )
    state = serializers.CharField(
        read_only=True,
        help_text="Connection state: 'running', 'blocked', 'blocking'",
    )
    recv_oct = serializers.IntegerField(
        read_only=True,
        help_text="Bytes received on this connection",
    )
    send_oct = serializers.IntegerField(
        read_only=True,
        help_text="Bytes sent on this connection",
    )
    channels = serializers.IntegerField(
        read_only=True,
        help_text="Number of channels on this connection",
    )
    timeout = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Heartbeat timeout in seconds",
    )
    client_properties = RmqClientPropertiesSerializer(
        read_only=True,
        allow_null=True,
        help_text="Client identification properties",
    )


class RmqEnrichedUserStatsItemSerializer(serializers.Serializer):
    """Serializer for RabbitMQ user with enriched connection details."""

    username = serializers.CharField(
        read_only=True,
        help_text="RabbitMQ username (corresponds to EventSubscription UUID)",
    )
    connections = RmqEnrichedConnectionSerializer(
        many=True,
        read_only=True,
        help_text="List of active connections with detailed statistics",
    )


class RmqEnrichedUserStatsSerializer(serializers.ListSerializer):
    """List serializer for enriched RabbitMQ user statistics."""

    child = RmqEnrichedUserStatsItemSerializer()


# RabbitMQ Overview Serializers (Part C)


class RmqMessageStatsSerializer(serializers.Serializer):
    """Serializer for RabbitMQ message throughput statistics."""

    publish = serializers.IntegerField(
        read_only=True,
        help_text="Total messages published",
    )
    publish_rate = serializers.FloatField(
        read_only=True,
        help_text="Messages published per second",
    )
    deliver = serializers.IntegerField(
        read_only=True,
        help_text="Total messages delivered to consumers",
    )
    deliver_rate = serializers.FloatField(
        read_only=True,
        help_text="Messages delivered per second",
    )
    confirm = serializers.IntegerField(
        read_only=True,
        help_text="Total messages confirmed by broker",
    )
    confirm_rate = serializers.FloatField(
        read_only=True,
        help_text="Messages confirmed per second",
    )
    ack = serializers.IntegerField(
        read_only=True,
        help_text="Total messages acknowledged by consumers",
    )
    ack_rate = serializers.FloatField(
        read_only=True,
        help_text="Messages acknowledged per second",
    )


class RmqQueueTotalsSerializer(serializers.Serializer):
    """Serializer for RabbitMQ global queue message totals."""

    messages = serializers.IntegerField(
        read_only=True,
        help_text="Total messages across all queues",
    )
    messages_ready = serializers.IntegerField(
        read_only=True,
        help_text="Messages ready for delivery",
    )
    messages_unacknowledged = serializers.IntegerField(
        read_only=True,
        help_text="Messages awaiting acknowledgement",
    )


class RmqObjectTotalsSerializer(serializers.Serializer):
    """Serializer for RabbitMQ object counts."""

    connections = serializers.IntegerField(
        read_only=True,
        help_text="Total active connections",
    )
    channels = serializers.IntegerField(
        read_only=True,
        help_text="Total active channels",
    )
    exchanges = serializers.IntegerField(
        read_only=True,
        help_text="Total exchanges",
    )
    queues = serializers.IntegerField(
        read_only=True,
        help_text="Total queues",
    )
    consumers = serializers.IntegerField(
        read_only=True,
        help_text="Total active consumers",
    )


class RmqListenerSerializer(serializers.Serializer):
    """Serializer for RabbitMQ protocol listener."""

    protocol = serializers.CharField(
        read_only=True,
        help_text="Protocol name (e.g., 'amqp', 'http', 'clustering')",
    )
    port = serializers.IntegerField(
        read_only=True,
        help_text="Listening port number",
    )


class RmqOverviewSerializer(serializers.Serializer):
    """Serializer for RabbitMQ cluster overview statistics."""

    cluster_name = serializers.CharField(
        read_only=True,
        help_text="Name of the RabbitMQ cluster",
    )
    rabbitmq_version = serializers.CharField(
        read_only=True,
        help_text="RabbitMQ server version",
    )
    erlang_version = serializers.CharField(
        read_only=True,
        help_text="Erlang/OTP runtime version",
    )
    message_stats = RmqMessageStatsSerializer(
        read_only=True,
        help_text="Message throughput statistics with rates",
    )
    queue_totals = RmqQueueTotalsSerializer(
        read_only=True,
        help_text="Global queue message counts",
    )
    object_totals = RmqObjectTotalsSerializer(
        read_only=True,
        help_text="Counts of connections, channels, queues, etc.",
    )
    node = serializers.CharField(
        read_only=True,
        help_text="Current RabbitMQ node name",
    )
    listeners = RmqListenerSerializer(
        many=True,
        read_only=True,
        help_text="Active protocol listeners",
    )
