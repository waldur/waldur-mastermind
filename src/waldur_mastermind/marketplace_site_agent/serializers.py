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
