"""
OpenAPI schema definitions for metadata endpoints.
This ensures proper enum types are exported to TypeScript.
"""

from rest_framework import serializers

# Import enum classes to generate proper OpenAPI types
from waldur_core.logging.enums import EventGroup, EventType
from waldur_core.permissions.enums import PermissionEnum, RoleEnum


# Create enum fields that export all values automatically
class WaldurRoleEnumField(serializers.ChoiceField):
    """Role enum field for OpenAPI schema generation."""

    def __init__(self, **kwargs):
        choices = [(role.value, role.value) for role in RoleEnum]
        super().__init__(choices=choices, **kwargs)


class WaldurPermissionEnumField(serializers.ChoiceField):
    """Permission enum field for OpenAPI schema generation."""

    def __init__(self, **kwargs):
        choices = [(perm.value, perm.value) for perm in PermissionEnum]
        super().__init__(choices=choices, **kwargs)


class WaldurEventTypeEnumField(serializers.ChoiceField):
    """Event type enum field for OpenAPI schema generation."""

    def __init__(self, **kwargs):
        choices = [(event.value, event.value) for event in EventType]
        super().__init__(choices=choices, **kwargs)


class WaldurEventGroupEnumField(serializers.ChoiceField):
    """Event group enum field for OpenAPI schema generation."""

    def __init__(self, **kwargs):
        choices = [(group.value, group.value) for group in EventGroup]
        super().__init__(choices=choices, **kwargs)


# Metadata response serializers
class PermissionMetadataResponseSerializer(serializers.Serializer):
    """Permission metadata including all roles and permissions"""

    roles = serializers.DictField(
        child=WaldurRoleEnumField(), help_text="Map of role keys to role enum values"
    )
    permissions = serializers.DictField(
        child=WaldurPermissionEnumField(),
        help_text="Map of permission keys to permission enum values",
    )
    permission_map = serializers.DictField(
        child=WaldurPermissionEnumField(),
        help_text="Map of resource types to create permission enums",
    )
    permission_descriptions = serializers.ListField(
        child=serializers.DictField(),
        help_text="Grouped permission descriptions for UI",
    )


class EventMetadataResponseSerializer(serializers.Serializer):
    """Event metadata including all event groups and types"""

    event_groups = serializers.DictField(
        child=serializers.ListField(child=WaldurEventTypeEnumField()),
        help_text="Map of event group keys to lists of event type enums",
    )


class FeatureMetadataResponseSerializer(serializers.Serializer):
    """Feature metadata including all features and toggles"""

    features = serializers.ListField(
        child=serializers.DictField(),
        help_text="List of feature sections with descriptions",
    )
    feature_enums = serializers.DictField(
        child=serializers.DictField(child=serializers.CharField()),
        help_text="Nested feature enum values by section",
    )


class SettingsMetadataResponseSerializer(serializers.Serializer):
    """Settings metadata from Constance configuration"""

    settings = serializers.ListField(
        child=serializers.DictField(),
        help_text="List of settings sections with configuration items",
    )
