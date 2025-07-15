import re

from rest_framework import serializers

from waldur_autoprovisioning import models
from waldur_core.permissions.models import Role
from waldur_mastermind.marketplace import models as marketplace_models


class RuleSerializer(serializers.HyperlinkedModelSerializer):
    project_role = serializers.HyperlinkedRelatedField(
        queryset=Role.project_roles(),
        view_name="role-detail",
        lookup_field="uuid",
        required=False,
        allow_null=True,
    )
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_uuid = serializers.CharField(source="customer.uuid", read_only=True)

    project_role_description = serializers.CharField(
        source="project_role.description", read_only=True
    )
    project_role_name = serializers.CharField(
        required=False, allow_null=True, write_only=True
    )
    project_role_dispay_name = serializers.CharField(
        source="project_role.name", read_only=True
    )
    plan = serializers.HyperlinkedRelatedField(
        view_name="marketplace-plan-detail",
        lookup_field="uuid",
        queryset=marketplace_models.Plan.objects.all(),
        required=False,
        allow_null=True,
    )
    plan_attributes = serializers.DictField(
        required=False,
        default=dict,
    )
    plan_limits = serializers.DictField(
        required=False,
        default=dict,
    )
    user_affiliations = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    user_email_patterns = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )

    class Meta:
        model = models.Rule
        fields = (
            "name",
            "uuid",
            "url",
            "user_affiliations",
            "user_email_patterns",
            "customer",
            "customer_name",
            "customer_uuid",
            "project_role",
            "project_role_name",  # used for accepting role name to set
            "project_role_dispay_name",  # used for displaying the role name
            "project_role_description",
            "plan",
            "plan_attributes",
            "plan_limits",
        )
        extra_kwargs = {
            "url": {
                "view_name": "autoprovisioning-rule-detail",
                "lookup_field": "uuid",
            },
            "customer": {
                "view_name": "customer-detail",
                "lookup_field": "uuid",
            },
            "plan": {
                "view_name": "marketplace-plan-detail",
                "lookup_field": "uuid",
            },
        }

    def validate_user_email_patterns(self, value):
        """Validate that all email patterns are valid regex patterns."""
        if not value:
            return value

        invalid_patterns = []
        for pattern in value:
            if not pattern or not isinstance(pattern, str):
                invalid_patterns.append(pattern)
                continue
            try:
                re.compile(pattern)
            except re.error:
                invalid_patterns.append(pattern)

        if invalid_patterns:
            raise serializers.ValidationError(
                f"Invalid regex patterns: {invalid_patterns}"
            )

        return value

    def validate(self, attrs):
        project_role = attrs.get("project_role")
        project_role_name = attrs.get("project_role_name")

        # Treat empty string as None for project_role_name
        if project_role_name == "":
            project_role_name = None
            attrs.pop("project_role_name", None)

        # Check that exactly one of project_role or project_role_name is provided
        if project_role and project_role_name:
            raise serializers.ValidationError(
                "Cannot specify both project_role and project_role_name. Choose one."
            )

        # Require at least one role specification for both creation and updates
        if not project_role and not project_role_name:
            raise serializers.ValidationError(
                "Either project_role or project_role_name must be provided."
            )

        # If project_role_name is provided, look up the role by name
        if project_role_name:
            try:
                role = Role.project_roles().get(name=project_role_name)
                attrs["project_role"] = role
            except Role.DoesNotExist:
                raise serializers.ValidationError(
                    f"Project role with name '{project_role_name}' does not exist."
                )
            # Remove project_role_name from attrs as it's not a model field
            attrs.pop("project_role_name", None)

        return attrs
