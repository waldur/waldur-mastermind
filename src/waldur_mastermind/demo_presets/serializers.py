from rest_framework import serializers

from waldur_mastermind.marketplace import models


class DemoPresetSerializer(serializers.Serializer):
    """Serializer for demo preset metadata."""

    name = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    version = serializers.CharField(read_only=True)
    entity_counts = serializers.DictField(
        child=serializers.IntegerField(),
        read_only=True,
    )
    scenarios = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )


class DemoPresetPreviewRoleSerializer(serializers.Serializer):
    role = serializers.CharField(help_text="Role name, such as CUSTOMER.OWNER.")
    scope_type = serializers.CharField(
        help_text="Type of the role's scope, such as structure.customer."
    )
    scope_name = serializers.CharField(
        allow_blank=True, help_text="Name of the role's scope in the preset."
    )


class DemoPresetPreviewUserSerializer(serializers.Serializer):
    username = serializers.CharField()
    email = serializers.CharField(allow_blank=True)
    full_name = serializers.CharField(allow_blank=True)
    is_staff = serializers.BooleanField()
    is_support = serializers.BooleanField()
    is_active = serializers.BooleanField(
        help_text="An inactive user is imported but cannot log in."
    )
    password = serializers.CharField(
        allow_null=True,
        help_text="The preset's password for this user. Never given for staff, "
        "support or inactive users, or for the overlay's users.",
    )
    roles = DemoPresetPreviewRoleSerializer(many=True)


class DemoPresetPreviewProjectSerializer(serializers.Serializer):
    name = serializers.CharField()
    resources = serializers.ListField(
        child=serializers.CharField(), help_text="Names of the project's resources."
    )


class DemoPresetPreviewOrganizationSerializer(serializers.Serializer):
    name = serializers.CharField()
    projects = DemoPresetPreviewProjectSerializer(many=True)


class DemoPresetPreviewOfferingGroupSerializer(serializers.Serializer):
    type = serializers.CharField(allow_blank=True)
    category = serializers.CharField(allow_blank=True)
    names = serializers.ListField(child=serializers.CharField())


class DemoPresetPreviewOverlayContentsSerializer(serializers.Serializer):
    users = DemoPresetPreviewUserSerializer(many=True)
    organizations = DemoPresetPreviewOrganizationSerializer(many=True)
    offerings = DemoPresetPreviewOfferingGroupSerializer(many=True)


class DemoPresetPreviewSettingChangeSerializer(serializers.Serializer):
    key = serializers.CharField()
    current_value = serializers.JSONField(
        allow_null=True, help_text="Null for a secret."
    )
    new_value = serializers.JSONField(
        allow_null=True, help_text="Value after the load. Null for a secret."
    )
    source = serializers.ChoiceField(
        choices=["preset", "overlay", "default", "deployment"],
        help_text="Where the value after the load comes from: the preset, the "
        "deployment's overlay preset, the setting's default, or the deployment, "
        "which owns the setting.",
    )
    changed = serializers.BooleanField(help_text="Whether the load changes the value.")
    skipped = serializers.BooleanField(
        help_text="The preset sets a setting the deployment owns; it keeps its value."
    )
    secret = serializers.BooleanField(help_text="Values of secrets are never shown.")


class DemoPresetPreviewFeatureChangeSerializer(serializers.Serializer):
    key = serializers.CharField()
    current_value = serializers.BooleanField()
    new_value = serializers.BooleanField()
    source = serializers.ChoiceField(
        choices=["preset", "overlay", "default"],
        help_text="Where the value after the load comes from.",
    )
    changed = serializers.BooleanField()


class DemoPresetPreviewSerializer(DemoPresetSerializer):
    """What loading a demo preset would do. Setting and feature changes
    describe a load that cleans up first, the default."""

    overlay = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="The deployment's overlay preset imported after this one, if any.",
    )
    users = DemoPresetPreviewUserSerializer(many=True, read_only=True)
    organizations = DemoPresetPreviewOrganizationSerializer(many=True, read_only=True)
    offerings = DemoPresetPreviewOfferingGroupSerializer(
        many=True,
        read_only=True,
        help_text="The preset's offerings, grouped by type and category.",
    )
    overlay_contents = DemoPresetPreviewOverlayContentsSerializer(
        read_only=True,
        allow_null=True,
        help_text="What the deployment's overlay preset adds after this one.",
    )
    settings_changes = DemoPresetPreviewSettingChangeSerializer(
        many=True,
        read_only=True,
        help_text="Settings the load changes, and every setting the preset sets.",
    )
    feature_changes = DemoPresetPreviewFeatureChangeSerializer(
        many=True,
        read_only=True,
        help_text="Features the load changes, and every feature the preset sets.",
    )
    warnings = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="What would not work after the load, such as a feature whose "
        "deployment configuration is missing.",
    )


class DemoPresetLoadRequestSerializer(serializers.Serializer):
    """Request serializer for loading a demo preset."""

    dry_run = serializers.BooleanField(
        default=False,
        help_text="Preview changes without applying them",
    )
    cleanup_first = serializers.BooleanField(
        default=True,
        help_text="Clean up existing data before loading the preset",
    )
    skip_users = serializers.BooleanField(
        default=False,
        help_text="Skip user import/cleanup",
    )
    skip_roles = serializers.BooleanField(
        default=False,
        help_text="Skip role import/cleanup",
    )


class DemoPresetUserSerializer(serializers.Serializer):
    """Serializer for demo preset user credentials."""

    username = serializers.CharField()
    password = serializers.CharField()
    email = serializers.CharField(required=False, allow_blank=True)
    is_staff = serializers.BooleanField(default=False)
    is_support = serializers.BooleanField(default=False)


class DemoPresetLoadResponseSerializer(serializers.Serializer):
    """Response serializer for demo preset loading."""

    success = serializers.BooleanField()
    message = serializers.CharField()
    output = serializers.CharField(required=False, allow_blank=True)
    users = DemoPresetUserSerializer(many=True, required=False)


class DemoPresetLoadJobSerializer(serializers.ModelSerializer):
    """A demo preset load running, or run, in the background."""

    class Meta:
        model = models.DemoPresetLoad
        fields = (
            "uuid",
            "preset",
            "state",
            "created",
            "started_at",
            "finished_at",
            "error_message",
        )
        read_only_fields = fields


class DemoPresetStatusSerializer(serializers.Serializer):
    """Public status of demo preset loading, for a browser following a reset."""

    loading = serializers.BooleanField(
        help_text="Whether a load is pending or running; the API answers 503 meanwhile."
    )
    load = DemoPresetLoadJobSerializer(
        allow_null=True, help_text="The latest load, if any."
    )
    users = DemoPresetUserSerializer(
        many=True,
        help_text="The loaded preset's users without a staff or support role, for quick login, once the latest load is done.",
    )
