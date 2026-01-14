from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.logging import utils as logging_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING
from waldur_mastermind.marketplace_site_agent import enums, models


class AgentProcessorSerializer(serializers.HyperlinkedModelSerializer):
    service = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.AgentService.objects.all(),
    )
    service_name = serializers.CharField(source="service.name", read_only=True)

    class Meta:
        model = models.AgentProcessor
        fields = (
            "uuid",
            "url",
            "service",
            "service_name",
            "name",
            "last_run",
            "backend_type",
            "backend_version",
            "created",
            "modified",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-site-agent-processor-detail",
            },
        }


class NestedAgentProcessorSerializer(AgentProcessorSerializer):
    class Meta:
        model = models.AgentProcessor
        fields = (
            "uuid",
            "url",
            "name",
            "last_run",
            "backend_type",
            "backend_version",
            "created",
            "modified",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-site-agent-processor-detail",
            },
        }


class AgentProcessorCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.AgentProcessor
        fields = ("name", "backend_type", "backend_version")


class AgentServiceSerializer(serializers.HyperlinkedModelSerializer):
    identity = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.AgentIdentity.objects.all(),
    )
    identity_name = serializers.CharField(source="identity.name", read_only=True)
    state = serializers.SerializerMethodField()
    processors = NestedAgentProcessorSerializer(
        many=True, read_only=True, source="agentprocessor_set"
    )

    class Meta:
        model = models.AgentService
        fields = (
            "uuid",
            "url",
            "identity",
            "identity_name",
            "name",
            "mode",
            "state",
            "statistics",
            "created",
            "modified",
            "processors",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-site-agent-service-detail",
            },
        }

    @extend_schema_field(
        serializers.ChoiceField(choices=enums.AgentServiceState.VALUES)
    )
    def get_state(self, service: models.AgentService) -> str:
        return service.get_state_display()


class NestedAgentServiceSerializer(AgentServiceSerializer):
    class Meta:
        model = models.AgentService
        fields = (
            "uuid",
            "url",
            "name",
            "mode",
            "state",
            "statistics",
            "created",
            "modified",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-site-agent-service-detail",
            },
        }


class AgentServiceCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.AgentService
        fields = ("name", "mode")


class AgentServiceStatisticsSerializer(serializers.Serializer):
    statistics = serializers.JSONField(
        help_text="Statistics data to be stored for the service"
    )


class AgentIdentitySerializer(serializers.HyperlinkedModelSerializer):
    offering = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=marketplace_models.Offering.objects.filter(type=SITE_AGENT_OFFERING),
        help_text="UUID of an offering with type 'Marketplace.Slurm'. "
        "Only site-agent offerings are accepted.",
    )
    services = NestedAgentServiceSerializer(
        many=True, read_only=True, source="agentservice_set"
    )

    class Meta:
        model = models.AgentIdentity
        fields = (
            "uuid",
            "url",
            "offering",
            "name",
            "version",
            "dependencies",
            "config_file_path",
            "config_file_content",
            "last_restarted",
            "created",
            "modified",
            "services",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-site-agent-identity-detail",
            },
        }


class AgentEventSubscriptionCreateSerializer(serializers.Serializer):
    observable_object_type = serializers.ChoiceField(
        choices=[
            (member.value, member.value)
            for member in logging_utils.ObservableObjectType
        ],
        help_text="The type of object to observe for events",
    )
    description = serializers.CharField(
        max_length=500,
        required=False,
        help_text="Optional description for the event subscription",
    )


class CleanupRequestSerializer(serializers.Serializer):
    dry_run = serializers.BooleanField(
        default=True,
        help_text="If true, only return what would be deleted without actually deleting",
    )
    older_than_hours = serializers.IntegerField(
        default=24,
        min_value=1,
        help_text="Delete entries older than this many hours",
    )


class CleanupResponseSerializer(serializers.Serializer):
    deleted_count = serializers.IntegerField(
        help_text="Number of items deleted (or would be deleted in dry run)"
    )
    dry_run = serializers.BooleanField(help_text="Whether this was a dry run")
    items = serializers.ListField(
        child=serializers.DictField(),
        help_text="List of deleted (or to-be-deleted) items",
    )


class AgentStatsResponseSerializer(serializers.Serializer):
    identities = serializers.DictField(help_text="Statistics about agent identities")
    services = serializers.DictField(help_text="Statistics about agent services")
    processors = serializers.DictField(help_text="Statistics about agent processors")


class AgentTaskStatsResponseSerializer(serializers.Serializer):
    active_tasks = serializers.ListField(
        child=serializers.DictField(),
        help_text="Currently running agent-related tasks",
    )
    scheduled_tasks = serializers.ListField(
        child=serializers.DictField(),
        help_text="Scheduled agent-related tasks",
    )
    reserved_tasks = serializers.ListField(
        child=serializers.DictField(),
        help_text="Reserved agent-related tasks",
    )
    error = serializers.CharField(
        required=False,
        help_text="Error message if task inspection failed",
    )


# Agent Connection Stats Serializers (Part B)


class AgentRmqConnectionSerializer(serializers.Serializer):
    """Serializer for RabbitMQ connection status of an agent subscription."""

    connected = serializers.BooleanField(
        read_only=True,
        help_text="Whether the agent has an active connection",
    )
    source_ip = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Client IP address of active connection",
    )
    connected_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text="Connection establishment timestamp",
    )
    state = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Connection state: 'running', 'blocked', 'blocking'",
    )
    recv_oct = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Bytes received on this connection",
    )
    send_oct = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="Bytes sent on this connection",
    )


class AgentEventSubscriptionWithConnectionSerializer(serializers.Serializer):
    """Serializer for event subscription with RabbitMQ connection status."""

    uuid = serializers.UUIDField(
        read_only=True,
        help_text="Event subscription UUID",
    )
    created = serializers.DateTimeField(
        read_only=True,
        help_text="When the subscription was created",
    )
    observable_objects = serializers.JSONField(
        read_only=True,
        help_text="List of observable object configurations",
    )
    rmq_connection = AgentRmqConnectionSerializer(
        read_only=True,
        allow_null=True,
        help_text="RabbitMQ connection status for this subscription",
    )


class AgentQueueInfoSerializer(serializers.Serializer):
    """Serializer for RabbitMQ queue information."""

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
        help_text="Number of active consumers",
    )
    object_type = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Parsed object type from queue name",
    )


class AgentServiceStatusSerializer(serializers.Serializer):
    """Serializer for agent service status in connection stats."""

    uuid = serializers.UUIDField(
        read_only=True,
        help_text="Service UUID",
    )
    name = serializers.CharField(
        read_only=True,
        help_text="Service name",
    )
    state = serializers.CharField(
        read_only=True,
        help_text="Service state: ACTIVE, IDLE, or ERROR",
    )
    modified = serializers.DateTimeField(
        read_only=True,
        help_text="Last modification timestamp",
    )


class AgentConnectionInfoSerializer(serializers.Serializer):
    """Serializer for individual agent connection status."""

    uuid = serializers.UUIDField(
        read_only=True,
        help_text="Agent identity UUID",
    )
    name = serializers.CharField(
        read_only=True,
        help_text="Agent name",
    )
    offering_uuid = serializers.UUIDField(
        read_only=True,
        help_text="Associated offering UUID",
    )
    offering_name = serializers.CharField(
        read_only=True,
        help_text="Associated offering name",
    )
    version = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Agent version",
    )
    last_restarted = serializers.DateTimeField(
        read_only=True,
        help_text="When the agent was last restarted",
    )
    services = AgentServiceStatusSerializer(
        many=True,
        read_only=True,
        help_text="Services running within this agent",
    )
    event_subscriptions = AgentEventSubscriptionWithConnectionSerializer(
        many=True,
        read_only=True,
        help_text="Event subscriptions with connection status",
    )
    queues = AgentQueueInfoSerializer(
        many=True,
        read_only=True,
        help_text="RabbitMQ queues for this agent's offering",
    )


class AgentConnectionSummarySerializer(serializers.Serializer):
    """Serializer for agent connection stats summary."""

    total_agents = serializers.IntegerField(
        read_only=True,
        help_text="Total number of registered agents",
    )
    connected_agents = serializers.IntegerField(
        read_only=True,
        help_text="Number of agents with active RMQ connections",
    )
    disconnected_agents = serializers.IntegerField(
        read_only=True,
        help_text="Number of agents without active connections",
    )
    total_queued_messages = serializers.IntegerField(
        read_only=True,
        help_text="Total messages across all agent queues",
    )


class AgentConnectionStatsResponseSerializer(serializers.Serializer):
    """Response serializer for agent connection statistics."""

    agents = AgentConnectionInfoSerializer(
        many=True,
        read_only=True,
        help_text="List of agents with connection status",
    )
    summary = AgentConnectionSummarySerializer(
        read_only=True,
        help_text="Summary statistics",
    )
