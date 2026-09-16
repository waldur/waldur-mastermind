from rest_framework import serializers

from waldur_core.permissions.models import Role
from waldur_core.structure.models import Project

from . import models, rules


def _string_list(value, field_name):
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise serializers.ValidationError(
            f"{field_name} must be a list of non-empty strings."
        )
    return sorted({item.strip() for item in value})


class SramProjectRuleSerializer(serializers.HyperlinkedModelSerializer):
    project_role = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=Role.objects.filter(content_type__model="project", is_active=True),
    )
    project_role_name = serializers.CharField(
        source="project_role.name", read_only=True
    )
    project_role_description = serializers.CharField(
        source="project_role.description", read_only=True
    )
    labels = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    group_short_name_patterns = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )

    class Meta:
        model = models.SramProjectRule
        fields = (
            "url",
            "uuid",
            "name",
            "is_active",
            "source_kind",
            "labels",
            "group_short_name_patterns",
            "project_field",
            "project_match",
            "project_pattern",
            "project_role",
            "project_role_name",
            "project_role_description",
            "created",
            "modified",
        )
        read_only_fields = ("uuid", "created", "modified")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "sram-project-rule-detail"},
        }

    def validate_labels(self, value):
        return _string_list(value, "labels")

    def validate_group_short_name_patterns(self, value):
        return _string_list(value, "group_short_name_patterns")

    def validate(self, attrs):
        instance = self.instance
        pattern = attrs.get(
            "project_pattern", instance.project_pattern if instance else None
        )
        if pattern is None:
            pattern = models.SramProjectRule._meta.get_field(
                "project_pattern"
            ).get_default()
        match = attrs.get(
            "project_match",
            instance.project_match
            if instance
            else models.SramProjectRule.MatchType.PREFIX,
        )
        try:
            rules.validate_pattern(pattern, match)
        except ValueError as exc:
            raise serializers.ValidationError({"project_pattern": str(exc)})

        kind = attrs.get(
            "source_kind",
            instance.source_kind
            if instance
            else models.SramProjectRule.SourceKind.COLLABORATION,
        )
        patterns = attrs.get(
            "group_short_name_patterns",
            instance.group_short_name_patterns if instance else [],
        )
        if patterns and kind == models.SramProjectRule.SourceKind.COLLABORATION:
            raise serializers.ValidationError(
                {
                    "group_short_name_patterns": "Group short names only apply to "
                    "groups; set source_kind to group or any."
                }
            )
        return attrs


class SramGroupSerializer(serializers.ModelSerializer):
    customer_uuid = serializers.UUIDField(
        source="customer.uuid", read_only=True, allow_null=True
    )
    customer_name = serializers.CharField(
        source="customer.name", read_only=True, allow_null=True
    )
    role_uuid = serializers.UUIDField(
        source="role.uuid", read_only=True, allow_null=True
    )
    role_name = serializers.CharField(
        source="role.name", read_only=True, allow_null=True
    )
    labels = serializers.ListField(child=serializers.CharField(), read_only=True)
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = models.SramGroup
        fields = (
            "uuid",
            "external_id",
            "display_name",
            "urn",
            "kind",
            "description",
            "labels",
            "customer_uuid",
            "customer_name",
            "role_uuid",
            "role_name",
            "member_count",
            "created",
            "modified",
        )
        read_only_fields = fields

    def get_member_count(self, group) -> int:
        return group.members.count()


class SramRulePreviewProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ("uuid", "name", "slug", "backend_id")


class SramRulePreviewUserSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    username = serializers.CharField()
    full_name = serializers.CharField()


class SramRulePreviewItemSerializer(serializers.Serializer):
    group = SramGroupSerializer()
    projects = SramRulePreviewProjectSerializer(many=True)
    users = SramRulePreviewUserSerializer(many=True)
