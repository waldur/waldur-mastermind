from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from waldur_core.logging import enums as logging_enums
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    SCRIPT_OFFERING,
    SITE_AGENT_OFFERING,
)
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


class AgentDependencySerializer(serializers.Serializer):
    package = serializers.CharField()
    version = serializers.CharField()


class AgentIdentitySerializer(serializers.HyperlinkedModelSerializer):
    offering = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=marketplace_models.Offering.objects.filter(
            type__in=[
                SITE_AGENT_OFFERING,
                SCRIPT_OFFERING,
                OPENSTACK_TENANT_OFFERING,
                BASIC_OFFERING,
            ]
        ),
        help_text="UUID of an offering with a site-agent compatible type.",
    )
    created_by = serializers.SlugRelatedField(
        slug_field="uuid",
        read_only=True,
        allow_null=True,
    )
    services = NestedAgentServiceSerializer(
        many=True, read_only=True, source="agentservice_set"
    )
    dependencies = AgentDependencySerializer(many=True, required=False)

    def validate_offering(self, value):
        # An agent identity's offering is fixed at creation. The field's queryset
        # is not scoped to the caller, and update (PUT) only gates on managing
        # the CURRENT offering — so allowing a change would let an offering
        # manager repoint the record onto an offering they don't control. A PUT
        # that re-sends the same offering (a full round-trip) is still fine.
        if self.instance is not None and value != self.instance.offering:
            raise serializers.ValidationError(
                "Offering cannot be changed after the agent identity is created."
            )
        return value

    class Meta:
        model = models.AgentIdentity
        fields = (
            "uuid",
            "url",
            "offering",
            "created_by",
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
            for member in logging_enums.ObservableObjectType
        ],
        help_text="The type of object to observe for events",
    )
    description = serializers.CharField(
        max_length=500,
        required=False,
        help_text="Optional description for the event subscription",
    )


class AgentQueueRegistrationSerializer(serializers.Serializer):
    # No `description` field: unlike the legacy EventSubscription, EventConsumer
    # has nowhere to store it and register_queue never read it — exposing it in
    # the schema implied a persistence that never happened.
    object_types = serializers.ListField(
        child=serializers.ChoiceField(
            choices=[
                (member.value, member.value)
                for member in logging_enums.ObservableObjectType
            ],
        ),
        required=False,
        # No default: an omitted field means "keep the current filter", an
        # explicit [] means "all types". With default=list an agent that simply
        # stopped sending the field on restart would silently widen its queue
        # from a narrow set back to the full firehose.
        help_text=(
            "List of observable object types to receive. An explicit empty list "
            "means all types; omitting the field leaves the current filter "
            "unchanged."
        ),
    )


class AgentQueueRegistrationResponseSerializer(serializers.Serializer):
    rmq_username = serializers.CharField(
        help_text="RabbitMQ username (UUID hex) for STOMP authentication",
    )
    queue_name = serializers.CharField(
        help_text="RabbitMQ queue name (consumer_{consumer_uuid})",
    )
    vhost = serializers.CharField(
        help_text="RabbitMQ virtual host (user UUID)",
    )
    observable_object_types = serializers.ListField(
        child=serializers.CharField(),
        help_text="List of observable object types routed to this queue",
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


class CleanupResponseItemSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()
    offering__name = serializers.CharField(required=False)
    created = serializers.DateTimeField(required=False)
    identity__name = serializers.CharField(required=False)
    state = serializers.CharField(required=False)
    modified = serializers.DateTimeField(required=False)


class CleanupResponseSerializer(serializers.Serializer):
    deleted_count = serializers.IntegerField(
        help_text="Number of items deleted (or would be deleted in dry run)"
    )
    dry_run = serializers.BooleanField(help_text="Whether this was a dry run")
    items = CleanupResponseItemSerializer(
        many=True,
        help_text="List of deleted (or to-be-deleted) items",
    )


class AgentStatsOfferingCountSerializer(serializers.Serializer):
    offering__name = serializers.CharField()
    offering__uuid = serializers.UUIDField()
    count = serializers.IntegerField()


class AgentStatsIdentitiesSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    by_offering = AgentStatsOfferingCountSerializer(many=True)


class AgentStatsServicesStateSerializer(serializers.Serializer):
    active = serializers.IntegerField()
    idle = serializers.IntegerField()
    error = serializers.IntegerField()


class AgentStatsServicesSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    by_state = AgentStatsServicesStateSerializer()
    stale_count = serializers.IntegerField()


class AgentStatsBackendTypeSerializer(serializers.Serializer):
    backend_type = serializers.CharField(allow_null=True)
    count = serializers.IntegerField()


class AgentStatsProcessorsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    by_backend_type = AgentStatsBackendTypeSerializer(many=True)
    stale_count = serializers.IntegerField()


class AgentStatsResponseSerializer(serializers.Serializer):
    identities = AgentStatsIdentitiesSerializer(
        help_text="Statistics about agent identities"
    )
    services = AgentStatsServicesSerializer(help_text="Statistics about agent services")
    processors = AgentStatsProcessorsSerializer(
        help_text="Statistics about agent processors"
    )


class ActiveAgentTaskSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    args = serializers.ListField(child=serializers.CharField(), required=False)
    worker = serializers.CharField()


class ScheduledAgentTaskSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    eta = serializers.CharField()


class ReservedAgentTaskSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()


class AgentTaskStatsResponseSerializer(serializers.Serializer):
    active_tasks = ActiveAgentTaskSerializer(
        many=True,
        help_text="Currently running agent-related tasks",
    )
    scheduled_tasks = ScheduledAgentTaskSerializer(
        many=True,
        help_text="Scheduled agent-related tasks",
    )
    reserved_tasks = ReservedAgentTaskSerializer(
        many=True,
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


class SiteAgentLogCreateSerializer(serializers.Serializer):
    """Input: one log entry. The agent sends a list of these."""

    agent_identity_uuid = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.AgentIdentity.objects.filter(
            offering__type__in=[
                SITE_AGENT_OFFERING,
                SCRIPT_OFFERING,
                OPENSTACK_TENANT_OFFERING,
                BASIC_OFFERING,
            ]
        ),
    )
    timestamp = serializers.FloatField()
    level = serializers.ChoiceField(choices=enums.LogLevel.CHOICES)
    message = serializers.CharField()
    module = serializers.CharField(max_length=255)

    def validate_agent_identity_uuid(self, agent_identity):
        request = self.context.get("request")
        if request:
            checked = self.context.setdefault("_checked_identity_pks", set())
            if agent_identity.pk not in checked:
                offering = agent_identity.offering
                can_push = has_permission(
                    request, PermissionEnum.CREATE_OFFERING, offering.customer
                ) or has_permission(request, PermissionEnum.UPDATE_OFFERING, offering)
                if not can_push:
                    raise PermissionDenied()
                checked.add(agent_identity.pk)
        return agent_identity


class SiteAgentLogSerializer(serializers.ModelSerializer):
    offering_uuid = serializers.UUIDField(
        source="agent_identity.offering.uuid", read_only=True
    )
    offering = serializers.HyperlinkedRelatedField(
        source="agent_identity.offering",
        view_name="marketplace-provider-offering-detail",
        read_only=True,
        lookup_field="uuid",
    )
    agent_identity_uuid = serializers.UUIDField(
        source="agent_identity.uuid",
        read_only=True,
    )

    class Meta:
        model = models.SiteAgentLog
        fields = (
            "uuid",
            "offering",
            "offering_uuid",
            "agent_identity_uuid",
            "timestamp",
            "level",
            "message",
            "module",
            "created",
        )
