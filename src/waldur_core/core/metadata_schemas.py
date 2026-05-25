"""
OpenAPI schema definitions for metadata endpoints.
This ensures proper enum types with all values are exported to TypeScript.
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

# Import enum classes from their source modules
from waldur_core.logging.enums import EventGroup, EventType
from waldur_core.permissions.enums import PermissionEnum, RoleEnum


# Enhanced enum fields that export all enum values for better TypeScript generation
class AllRoleEnumField(serializers.ChoiceField):
    """Role enum field that includes all possible role values for OpenAPI export."""

    def __init__(self, **kwargs):
        # Extract all role values from the RoleEnum class
        choices = [(role.value, role.value) for role in RoleEnum]
        super().__init__(choices=choices, **kwargs)


class AllPermissionEnumField(serializers.ChoiceField):
    """Permission enum field that includes all possible permission values for OpenAPI export."""

    def __init__(self, **kwargs):
        # Extract all permission values from the PermissionEnum class
        choices = [(perm.value, perm.value) for perm in PermissionEnum]
        super().__init__(choices=choices, **kwargs)


class AllEventTypeEnumField(serializers.ChoiceField):
    """Event type enum field that includes all possible event values for OpenAPI export."""

    def __init__(self, **kwargs):
        # Extract all event type values from the EventType enum class
        choices = [(event.value, event.value) for event in EventType]
        super().__init__(choices=choices, **kwargs)


class AllEventGroupEnumField(serializers.ChoiceField):
    """Event group enum field that includes all possible group values for OpenAPI export."""

    def __init__(self, **kwargs):
        # Extract all event group values from the EventGroup enum class
        choices = [(group.value, group.value) for group in EventGroup]
        super().__init__(choices=choices, **kwargs)


# Static enum serializers that will generate proper TypeScript constants
@extend_schema_field(
    serializers.DictField(child=serializers.ListField(child=AllEventTypeEnumField()))
)
class EventGroupsStaticSerializer(serializers.DictField):
    """
    Serializer that exports event groups as static constants.
    This generates TypeScript interfaces with all enum values.
    """

    def __init__(self, **kwargs):
        child = serializers.ListField(child=AllEventTypeEnumField())
        super().__init__(child=child, **kwargs)


@extend_schema_field(serializers.DictField(child=AllRoleEnumField()))
class RoleEnumStaticSerializer(serializers.DictField):
    """
    Serializer that exports role enums as static constants.
    Maps role keys to role enum values from RoleEnum.
    """

    def __init__(self, **kwargs):
        child = AllRoleEnumField()
        super().__init__(child=child, **kwargs)


@extend_schema_field(serializers.DictField(child=AllPermissionEnumField()))
class PermissionEnumStaticSerializer(serializers.DictField):
    """
    Serializer that exports permission enums as static constants.
    Maps permission keys to permission enum values from PermissionEnum.
    """

    def __init__(self, **kwargs):
        child = AllPermissionEnumField()
        super().__init__(child=child, **kwargs)


class PermissionDescriptionOptionSerializer(serializers.Serializer):
    label = serializers.CharField(help_text="Human-readable permission label")
    value = AllPermissionEnumField(help_text="Permission enum value")


class PermissionDescriptionSerializer(serializers.Serializer):
    label = serializers.CharField(help_text="Category name")
    options = PermissionDescriptionOptionSerializer(
        many=True, help_text="List of permissions in this category"
    )


# Enhanced metadata response serializers with complete enum information
class PermissionMetadataResponseSerializer(serializers.Serializer):
    """Permission metadata including all roles and permissions with complete enum values"""

    roles = RoleEnumStaticSerializer(
        help_text="Map of role keys to role enum values from RoleEnum"
    )
    permissions = PermissionEnumStaticSerializer(
        help_text="Map of permission keys to permission enum values from PermissionEnum"
    )
    permission_map = PermissionEnumStaticSerializer(
        help_text="Map of resource types to create permission enums"
    )
    permission_descriptions = PermissionDescriptionSerializer(
        many=True,
        help_text="Grouped permission descriptions for UI",
    )


class EventMetadataResponseSerializer(serializers.Serializer):
    """Event metadata including all event groups and types with complete enum values"""

    event_groups = EventGroupsStaticSerializer(
        help_text="Map of event group keys to lists of event type enums from EventType"
    )


class FeatureItemSerializer(serializers.Serializer):
    key = serializers.CharField()
    description = serializers.CharField()


class FeatureSectionSerializer(serializers.Serializer):
    key = serializers.CharField()
    description = serializers.CharField()
    items = FeatureItemSerializer(many=True)


class FeatureMetadataResponseSerializer(serializers.Serializer):
    """Feature metadata including all features and toggles"""

    features = FeatureSectionSerializer(many=True)
    feature_enums = serializers.DictField(
        child=serializers.DictField(child=serializers.CharField()),
        help_text="Nested feature enum values by section",
    )


class SettingsItemOptionSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()


class SettingsItemSerializer(serializers.Serializer):
    key = serializers.CharField()
    description = serializers.CharField()
    default = serializers.JSONField(required=False)
    type = serializers.CharField()
    options = SettingsItemOptionSerializer(many=True, required=False)


class SettingsSectionSerializer(serializers.Serializer):
    description = serializers.CharField()
    items = SettingsItemSerializer(many=True)


class SettingsMetadataResponseSerializer(serializers.Serializer):
    """Settings metadata from Constance configuration"""

    settings = SettingsSectionSerializer(many=True)
