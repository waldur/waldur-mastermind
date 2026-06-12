import logging
import uuid

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.core.fields import NaturalChoiceField
from waldur_core.core.serializers import RestrictedSerializerMixin
from waldur_core.logging import backend, enums, event_logger, models

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
        if type(instance) is models.BaseHook:
            for model in models.BaseHook.get_all_models():
                if model == models.BaseHook:
                    continue
                attr = model.__name__.lower()
                if hasattr(instance, attr):
                    instance = getattr(instance, attr)
                    break
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


class EventSubscriptionObservableObjectSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField(required=False)
    object_type = serializers.CharField()
    object_id = serializers.IntegerField(required=False)


@extend_schema_field(EventSubscriptionObservableObjectSerializer(many=True))
class ObservableObjectsField(serializers.JSONField):
    pass


class EventSubscriptionSerializer(serializers.HyperlinkedModelSerializer):
    observable_objects = ObservableObjectsField(
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

            object_types = [member.value for member in enums.ObservableObjectType]

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


class EventSubscriptionQueueSerializer(serializers.HyperlinkedModelSerializer):
    """Serializer for reading EventSubscriptionQueue instances."""

    queue_name = serializers.CharField(read_only=True)
    vhost = serializers.CharField(read_only=True)
    event_subscription_uuid = serializers.CharField(
        read_only=True, source="event_subscription.uuid.hex"
    )
    offering_uuid = serializers.CharField(read_only=True, source="offering_uuid.hex")

    class Meta:
        model = models.EventSubscriptionQueue
        fields = (
            "uuid",
            "url",
            "event_subscription",
            "event_subscription_uuid",
            "offering_uuid",
            "object_type",
            "queue_name",
            "vhost",
            "created",
        )
        read_only_fields = ("queue_name", "vhost", "event_subscription")
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "event-subscription-queue-detail",
            },
            "event_subscription": {
                "lookup_field": "uuid",
                "view_name": "event-subscription-detail",
            },
        }


class EventSubscriptionQueueCreateSerializer(serializers.Serializer):
    """Serializer for creating EventSubscriptionQueue instances."""

    offering_uuid = serializers.UUIDField(
        help_text="UUID of the offering to receive events for"
    )
    object_type = serializers.ChoiceField(
        choices=[(t.value, t.value) for t in enums.ObservableObjectType],
        help_text="Type of observable object (e.g., 'resource', 'order')",
    )

    def validate_offering_uuid(self, value):
        """Verify user has access to this offering."""
        from waldur_mastermind.marketplace import enums as marketplace_enums
        from waldur_mastermind.marketplace import models as marketplace_models

        request = self.context.get("request")
        if not request or not request.user:
            raise serializers.ValidationError("Authentication required")

        offering_exists = marketplace_models.Offering.objects.filter(
            uuid=value
        ).exists()
        if not offering_exists:
            raise serializers.ValidationError(
                f"Offering with UUID {value} does not exist"
            )

        # Check user has access to the offering via standard permissions
        user_offerings = marketplace_models.Offering.objects.all().filter_for_user(
            request.user
        )
        if user_offerings.filter(uuid=value).exists():
            return value

        # ISD identity managers can access non-archived/draft offerings
        # for STOMP event subscription queue creation
        if request.user.is_identity_manager and request.user.managed_isds:
            if marketplace_models.Offering.objects.filter(
                uuid=value,
                state__in=marketplace_enums.OfferingStates.ISD_ALLOWED_STATES,
            ).exists():
                return value

        raise serializers.ValidationError("You do not have access to this offering")

    @transaction.atomic
    def create(self, validated_data):
        event_subscription = self.context["event_subscription"]
        offering_uuid = validated_data["offering_uuid"]
        object_type = validated_data["object_type"]

        # Create the EventSubscriptionQueue in the database
        queue = models.EventSubscriptionQueue.objects.create(
            event_subscription=event_subscription,
            offering_uuid=offering_uuid,
            object_type=object_type,
        )

        # Create the queue in RabbitMQ with correct arguments
        rmq_backend = backend.RabbitMQManagementBackend()
        queue_created = rmq_backend.create_queue(
            vhost=queue.vhost,
            queue_name=queue.queue_name,
            durable=True,
            auto_delete=False,
            arguments=backend.SUBSCRIPTION_QUEUE_ARGUMENTS,
        )

        if not queue_created:
            logger.error(
                "Failed to create RabbitMQ queue '%s' in vhost '%s'",
                queue.queue_name,
                queue.vhost,
            )
            raise serializers.ValidationError(
                "Failed to create queue in RabbitMQ. Please try again."
            )

        logger.info(
            "Created subscription queue '%s' for subscription %s, offering %s, type %s",
            queue.queue_name,
            event_subscription.uuid.hex,
            offering_uuid,
            object_type,
        )

        return queue


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


class SystemLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.SystemLog
        fields = (
            "id",
            "created",
            "source",
            "instance",
            "level",
            "level_number",
            "logger_name",
            "message",
            "context",
        )
        read_only_fields = fields


class SystemLogStatsInstanceSerializer(serializers.Serializer):
    source = serializers.CharField(read_only=True)
    instance = serializers.CharField(read_only=True)
    count = serializers.IntegerField(read_only=True)


class SystemLogStatsResponseSerializer(serializers.Serializer):
    instances = SystemLogStatsInstanceSerializer(many=True, read_only=True)
    total_size_bytes = serializers.IntegerField(read_only=True)
    total_size_mb = serializers.FloatField(read_only=True)


class SystemLogInstanceSerializer(serializers.Serializer):
    source = serializers.CharField(read_only=True)
    instance = serializers.CharField(read_only=True)
    last_seen = serializers.DateTimeField(read_only=True)
    count = serializers.IntegerField(read_only=True)


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
        help_text="Client platform (e.g., 'Python 3.12')",
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


# Pubsub Debug API Serializers


class CircuitBreakerConfigSerializer(serializers.Serializer):
    """Serializer for circuit breaker configuration."""

    failure_threshold = serializers.IntegerField(
        read_only=True,
        help_text="Number of failures before opening circuit",
    )
    recovery_timeout = serializers.IntegerField(
        read_only=True,
        help_text="Seconds to wait before attempting recovery",
    )
    success_threshold = serializers.IntegerField(
        read_only=True,
        help_text="Successful calls needed in half-open state to close",
    )


class CircuitBreakerStateChangeSerializer(serializers.Serializer):
    """Serializer for circuit breaker state change history."""

    timestamp = serializers.FloatField(
        read_only=True,
        help_text="Unix timestamp of state change",
    )
    from_state = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Previous state",
    )
    to_state = serializers.CharField(
        read_only=True,
        help_text="New state",
    )
    reason = serializers.CharField(
        read_only=True,
        help_text="Reason for state change",
    )


class CircuitBreakerStatusSerializer(serializers.Serializer):
    """Serializer for circuit breaker full status."""

    state = serializers.CharField(
        read_only=True,
        help_text="Current state: closed, open, or half_open",
    )
    failure_count = serializers.IntegerField(
        read_only=True,
        help_text="Number of consecutive failures",
    )
    success_count = serializers.IntegerField(
        read_only=True,
        help_text="Successful calls since last state change",
    )
    last_failure_time = serializers.FloatField(
        read_only=True,
        allow_null=True,
        help_text="Unix timestamp of last failure",
    )
    last_state_change = serializers.FloatField(
        read_only=True,
        allow_null=True,
        help_text="Unix timestamp of last state change",
    )
    config = CircuitBreakerConfigSerializer(
        read_only=True,
        help_text="Circuit breaker configuration",
    )
    state_history = CircuitBreakerStateChangeSerializer(
        many=True,
        read_only=True,
        help_text="Recent state transitions (last 50)",
    )


class CircuitBreakerResetSerializer(serializers.Serializer):
    """Serializer for circuit breaker reset response."""

    status = serializers.CharField(
        read_only=True,
        help_text="Operation status",
    )
    state = serializers.CharField(
        read_only=True,
        help_text="New circuit breaker state after reset",
    )


class PublishingMetricsSerializer(serializers.Serializer):
    """Serializer for message publishing metrics."""

    messages_sent = serializers.IntegerField(
        read_only=True,
        help_text="Total messages successfully sent",
    )
    messages_failed = serializers.IntegerField(
        read_only=True,
        help_text="Total failed message attempts",
    )
    messages_retried = serializers.IntegerField(
        read_only=True,
        help_text="Messages that required retry",
    )
    messages_skipped = serializers.IntegerField(
        read_only=True,
        help_text="Messages skipped due to circuit breaker",
    )
    circuit_breaker_trips = serializers.IntegerField(
        read_only=True,
        help_text="Number of times circuit breaker opened",
    )
    rate_limiter_rejections = serializers.IntegerField(
        read_only=True,
        help_text="Messages rejected by rate limiter",
    )
    avg_publish_time_ms = serializers.FloatField(
        read_only=True,
        help_text="Average message publish latency in milliseconds",
    )
    last_publish_time = serializers.FloatField(
        read_only=True,
        allow_null=True,
        help_text="Unix timestamp of last publish attempt",
    )


class MessageStateCacheFilterSerializer(serializers.Serializer):
    """Serializer for message state cache filter params."""

    resource_uuid = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Filter by resource UUID",
    )
    message_type = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Filter by message type",
    )


class MessageStateCacheSerializer(serializers.Serializer):
    """Serializer for message state cache statistics."""

    cache_ttl = serializers.IntegerField(
        read_only=True,
        help_text="Cache TTL in seconds",
    )
    description = serializers.CharField(
        read_only=True,
        help_text="Cache description",
    )
    filter = MessageStateCacheFilterSerializer(
        read_only=True,
        help_text="Applied filters",
    )


class PubsubCircuitBreakerSummarySerializer(serializers.Serializer):
    """Serializer for circuit breaker summary in overview."""

    state = serializers.CharField(
        read_only=True,
        help_text="Current state: closed, open, or half_open",
    )
    healthy = serializers.BooleanField(
        read_only=True,
        help_text="Whether circuit breaker is in healthy state (closed)",
    )
    failure_count = serializers.IntegerField(
        read_only=True,
        help_text="Number of consecutive failures",
    )


class PubsubMetricsSummarySerializer(serializers.Serializer):
    """Serializer for metrics summary in overview."""

    messages_sent = serializers.IntegerField(
        read_only=True,
        help_text="Total messages sent",
    )
    messages_failed = serializers.IntegerField(
        read_only=True,
        help_text="Total messages failed",
    )
    failure_rate = serializers.CharField(
        read_only=True,
        help_text="Failure rate as percentage string",
    )
    avg_latency_ms = serializers.FloatField(
        read_only=True,
        help_text="Average publish latency in milliseconds",
    )


class PubsubOverviewSerializer(serializers.Serializer):
    """Serializer for pubsub system health overview."""

    health_status = serializers.CharField(
        read_only=True,
        help_text="Overall health: healthy, degraded, or critical",
    )
    issues = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="List of current issues affecting health",
    )
    circuit_breaker = PubsubCircuitBreakerSummarySerializer(
        read_only=True,
        help_text="Circuit breaker summary",
    )
    metrics = PubsubMetricsSummarySerializer(
        read_only=True,
        help_text="Publishing metrics summary",
    )
    last_updated = serializers.DateTimeField(
        read_only=True,
        help_text="Timestamp when overview was generated",
    )


class TopQueueSerializer(serializers.Serializer):
    """Serializer for top queue by message count."""

    vhost = serializers.CharField(
        read_only=True,
        help_text="Virtual host name",
    )
    name = serializers.CharField(
        read_only=True,
        help_text="Queue name",
    )
    messages = serializers.IntegerField(
        read_only=True,
        help_text="Number of messages in queue",
    )
    consumers = serializers.IntegerField(
        read_only=True,
        help_text="Number of consumers attached",
    )


class EventSubscriptionQueuesOverviewSerializer(serializers.Serializer):
    """Serializer for subscription queues overview."""

    total_vhosts = serializers.IntegerField(
        read_only=True,
        help_text="Total number of vhosts with subscription queues",
    )
    total_queues = serializers.IntegerField(
        read_only=True,
        help_text="Total number of subscription queues",
    )
    total_messages = serializers.IntegerField(
        read_only=True,
        help_text="Total messages across all subscription queues",
    )
    top_queues_by_messages = TopQueueSerializer(
        many=True,
        read_only=True,
        help_text="Top 10 queues by message count",
    )


class DLQQueueSerializer(serializers.Serializer):
    """Serializer for dead letter queue info."""

    vhost = serializers.CharField(
        read_only=True,
        help_text="Virtual host name",
    )
    queue_name = serializers.CharField(
        read_only=True,
        help_text="DLQ queue name",
    )
    messages = serializers.IntegerField(
        read_only=True,
        help_text="Total messages in DLQ",
    )
    messages_ready = serializers.IntegerField(
        read_only=True,
        help_text="Messages ready for delivery",
    )
    consumers = serializers.IntegerField(
        read_only=True,
        help_text="Number of consumers attached",
    )


class DeadLetterQueueSerializer(serializers.Serializer):
    """Serializer for dead letter queue statistics."""

    total_dlq_messages = serializers.IntegerField(
        read_only=True,
        help_text="Total messages across all DLQs",
    )
    dlq_count = serializers.IntegerField(
        read_only=True,
        help_text="Number of DLQ queues found",
    )
    dlq_queues = DLQQueueSerializer(
        many=True,
        read_only=True,
        help_text="List of DLQ queues with their statistics",
    )
    note = serializers.CharField(
        read_only=True,
        help_text="Informational note about DLQs",
    )
