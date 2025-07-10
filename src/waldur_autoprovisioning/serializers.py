import re

from rest_framework import serializers

from waldur_autoprovisioning import models
from waldur_core.permissions.models import Role


class RuleSerializer(serializers.HyperlinkedModelSerializer):
    project_role = serializers.HyperlinkedRelatedField(
        queryset=Role.project_roles(),
        view_name="role-detail",
        lookup_field="uuid",
        required=False,
        allow_null=True,
    )
    plans = serializers.HyperlinkedRelatedField(
        view_name="autoprovisioning-rule-plan-detail",
        lookup_field="uuid",
        read_only=True,
        many=True,
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
            "uuid",
            "url",
            "user_affiliations",
            "user_email_patterns",
            "customer",
            "project_role",
            "plans",
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


class RulePlansSerializer(serializers.HyperlinkedModelSerializer):
    attributes = serializers.DictField(
        required=False,
        default=dict,
    )
    limits = serializers.DictField(
        required=False,
        default=dict,
    )

    class Meta:
        model = models.RulePlans
        fields = ("uuid", "url", "rule", "plan", "attributes", "limits")
        extra_kwargs = {
            "url": {
                "view_name": "autoprovisioning-rule-plan-detail",
                "lookup_field": "uuid",
            },
            "rule": {
                "view_name": "autoprovisioning-rule-detail",
                "lookup_field": "uuid",
            },
            "plan": {
                "view_name": "marketplace-plan-detail",
                "lookup_field": "uuid",
            },
        }
