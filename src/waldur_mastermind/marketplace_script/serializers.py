from rest_framework import serializers

from waldur_core.core.fields import NaturalChoiceField
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import OrderTypes
from waldur_mastermind.marketplace_script import models as marketplace_script_models


class CommonSerializer(serializers.Serializer):
    attributes = serializers.JSONField(read_only=True)
    limits = serializers.JSONField(read_only=True)
    customer_uuid = serializers.UUIDField(
        read_only=True, source="project.customer.uuid"
    )
    customer_name = serializers.ReadOnlyField(source="project.customer.name")
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_name = serializers.ReadOnlyField(source="project.name")
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    offering_name = serializers.ReadOnlyField(source="offering.name")
    plan_uuid = serializers.UUIDField(read_only=True, source="plan.uuid")
    plan_name = serializers.ReadOnlyField(source="plan.name")
    plan_component_amounts = serializers.SerializerMethodField()

    def get_plan_component_amounts(self, obj):
        """Return plan component amounts as a dict {component_type: amount}.

        This returns PlanComponent.amount for each component in the plan,
        representing the quantity of each component included in this plan.
        """
        if obj.plan:
            return {
                comp.component.type: comp.amount
                for comp in obj.plan.components.select_related("component").all()
                if comp.component
            }
        return {}


class OrderSerializer(CommonSerializer):
    order_uuid = serializers.UUIDField(read_only=True, source="uuid")
    creator_email = serializers.ReadOnlyField(source="created_by.email")
    creator_username = serializers.ReadOnlyField(source="created_by.username")
    resource_uuid = serializers.UUIDField(read_only=True, source="resource.uuid")
    resource_name = serializers.ReadOnlyField(source="resource.name")
    resource_backend_id = serializers.ReadOnlyField(source="resource.backend_id")
    resource_backend_metadata = serializers.ReadOnlyField(
        source="resource.backend_metadata"
    )
    resource_attributes = serializers.JSONField(
        read_only=True, source="resource.attributes"
    )


class ResourceSerializer(CommonSerializer):
    resource_uuid = serializers.UUIDField(read_only=True, source="uuid")
    resource_name = serializers.ReadOnlyField(source="name")
    resource_backend_id = serializers.ReadOnlyField(source="backend_id")
    resource_backend_metadata = serializers.ReadOnlyField(source="backend_metadata")


class DryRunTypes(OrderTypes):
    PULL = 5
    CHOICES = OrderTypes.CHOICES + ((PULL, "Pull"),)

    @classmethod
    def get_type_display(cls, index):
        for choice in cls.CHOICES:
            if index == choice[0]:
                return choice[1].lower()

        return index


class DryRunSerializer(
    serializers.HyperlinkedModelSerializer,
):
    plan = serializers.HyperlinkedRelatedField(
        view_name="marketplace-plan-detail",
        lookup_field="uuid",
        queryset=models.Plan.objects.all(),
        write_only=True,
        allow_null=True,
        required=False,
    )
    type = NaturalChoiceField(
        choices=DryRunTypes.CHOICES,
        required=False,
        default=DryRunTypes.CREATE,
        write_only=True,
    )
    attributes = serializers.JSONField(required=False, write_only=True)
    get_state_display = serializers.CharField(read_only=True)

    class Meta:
        model = marketplace_script_models.DryRun
        fields = (
            "url",
            "uuid",
            "plan",
            "type",
            "attributes",
            "order_attributes",
            "order_type",
            "order_offering",
            "state",
            "get_state_display",
            "output",
            "created",
        )

        read_only_fields = (
            "order_attributes",
            "order_type",
            "state",
            "output",
            "uuid",
            "created",
        )

        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-script-async-dry-run-detail",
            },
            "order_offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        }


class PullMarketplaceScriptResourceSerializer(serializers.Serializer):
    resource_uuid = serializers.UUIDField()


class ScriptDryRunResponseSerializer(serializers.Serializer):
    output = serializers.CharField()


class ScriptAsyncDryRunResponseSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
