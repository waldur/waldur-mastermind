from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from . import models, providers


class CorrectiveActionSerializer(serializers.Serializer):
    """Serializer for corrective actions"""

    label = serializers.CharField()
    category = serializers.ChoiceField(
        choices=[(c.value, c.value) for c in providers.ActionCategory]
    )
    severity = serializers.ChoiceField(
        choices=[(s.value, s.value) for s in providers.ActionSeverity]
    )
    method = serializers.CharField(default="GET")
    api_endpoint = serializers.BooleanField(default=False)
    confirmation_required = serializers.BooleanField(default=False)
    permissions_required = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    metadata = serializers.DictField(required=False, default=dict)

    # Frontend routing support
    route_name = serializers.CharField(required=False, allow_null=True)
    route_params = serializers.DictField(required=False, default=dict)


class UserActionSerializer(serializers.ModelSerializer):
    """Serializer for user actions"""

    related_object_name = serializers.SerializerMethodField()
    related_object_type = serializers.SerializerMethodField()
    corrective_actions = serializers.SerializerMethodField()
    is_temporarily_silenced = serializers.BooleanField(read_only=True)
    is_effectively_silenced = serializers.BooleanField(read_only=True)
    days_until_due = serializers.SerializerMethodField()
    route_params = serializers.DictField(read_only=True)

    class Meta:
        model = models.UserAction
        fields = [
            "uuid",
            "action_type",
            "title",
            "description",
            "urgency",
            "due_date",
            "is_silenced",
            "silenced_until",
            "is_temporarily_silenced",
            "is_effectively_silenced",
            "created",
            "modified",
            "related_object_name",
            "related_object_type",
            "corrective_actions",
            "days_until_due",
            # New typed fields
            "route_name",
            "route_params",
            "project_name",
            "project_uuid",
            "organization_name",
            "organization_uuid",
            "offering_name",
            "offering_uuid",
            "offering_type",
            "resource_name",
            "resource_uuid",
            "order_type",
        ]
        read_only_fields = [
            "uuid",
            "created",
            "modified",
            "is_temporarily_silenced",
            "is_effectively_silenced",
        ]

    @extend_schema_field(serializers.CharField())
    def get_related_object_name(self, obj):
        """Get a human-readable name for the related object"""
        if hasattr(obj.related_object, "name"):
            return obj.related_object.name
        elif hasattr(obj.related_object, "title"):
            return obj.related_object.title
        return str(obj.related_object)

    @extend_schema_field(serializers.CharField())
    def get_related_object_type(self, obj):
        """Get the content type name of the related object"""
        return obj.content_type.name

    @extend_schema_field(CorrectiveActionSerializer(many=True))
    def get_corrective_actions(self, obj):
        """Get corrective actions filtered by user permissions"""
        user = self.context["request"].user
        actions = obj.get_corrective_actions_for_user(user)

        # Convert CorrectiveAction dataclass instances to dict for serialization
        action_dicts = []
        for action in actions:
            action_dict = {
                "label": action.label,
                "category": action.category.value,
                "severity": action.severity.value,
                "method": action.method,
                "api_endpoint": action.api_endpoint,
                "confirmation_required": action.confirmation_required,
                "permissions_required": action.permissions_required,
                "metadata": action.metadata,
                "route_name": action.route_name,
                "route_params": action.route_params,
            }
            action_dicts.append(action_dict)

        return action_dicts

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_days_until_due(self, obj):
        """Calculate days until the action is due"""
        if not obj.due_date:
            return None

        delta = obj.due_date.date() - timezone.now().date()
        return delta.days


class UserActionSummarySerializer(serializers.Serializer):
    """Serializer for action summary statistics"""

    total = serializers.IntegerField()
    by_urgency = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Map of urgency level to count of actions",
    )
    by_type = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Map of action type string to count of actions",
    )
    overdue = serializers.IntegerField()


class SilenceActionSerializer(serializers.Serializer):
    """Serializer for silencing actions"""

    duration_days = serializers.IntegerField(
        required=False,
        help_text="Duration in days to silence the action. If not provided, silences permanently.",
    )

    def validate_duration_days(self, value):
        if value is not None and (value < 1 or value > 365):
            raise serializers.ValidationError(
                "Duration must be between 1 and 365 days."
            )
        return value


class UserActionExecutionSerializer(serializers.ModelSerializer):
    """Serializer for action executions"""

    class Meta:
        model = models.UserActionExecution
        fields = [
            "id",
            "corrective_action_label",
            "executed_at",
            "success",
            "error_message",
            "execution_metadata",
        ]
        read_only_fields = ["id", "executed_at"]


class UserActionProviderSerializer(serializers.ModelSerializer):
    """Serializer for action providers"""

    class Meta:
        model = models.UserActionProvider
        fields = [
            "id",
            "app_name",
            "provider_class",
            "action_type",
            "is_enabled",
            "schedule",
            "last_execution",
            "last_execution_status",
        ]
        read_only_fields = ["id", "last_execution", "last_execution_status"]


class ExecuteActionSerializer(serializers.Serializer):
    """Serializer for executing corrective actions"""

    action_label = serializers.CharField(
        help_text="Label of the corrective action to execute"
    )


class UpdateActionsSerializer(serializers.Serializer):
    """Serializer for updating user actions"""

    provider_action_type = serializers.CharField(
        required=False,
        allow_null=True,
        help_text="Optional provider action type to update. If not provided, updates all providers.",
    )


class UpdateActionsResponseSerializer(serializers.Serializer):
    """Serializer for update actions response"""

    status = serializers.CharField()
    message = serializers.CharField()
    provider_action_type = serializers.CharField(required=False, allow_null=True)


class SilenceActionResponseSerializer(serializers.Serializer):
    """Serializer for silence action response"""

    status = serializers.CharField()
    duration_days = serializers.IntegerField(required=False, allow_null=True)


class UnsilenceActionResponseSerializer(serializers.Serializer):
    """Serializer for unsilence action response"""

    status = serializers.CharField()


class ExecuteActionResponseSerializer(serializers.Serializer):
    """Serializer for execute action response"""

    action = serializers.CharField()
    message = serializers.CharField(required=False)
    redirect_url = serializers.URLField(required=False)
    metadata = serializers.DictField(required=False)


class ExecuteActionErrorResponseSerializer(serializers.Serializer):
    """Serializer for execute action error response"""

    error = serializers.CharField()


class BulkSilenceResponseSerializer(serializers.Serializer):
    """Serializer for bulk silence response"""

    status = serializers.CharField()
    count = serializers.IntegerField()
    duration_days = serializers.IntegerField(required=False, allow_null=True)


class SendNotificationResponseSerializer(serializers.Serializer):
    """Serializer for send notification response"""

    status = serializers.CharField()
    message = serializers.CharField()
