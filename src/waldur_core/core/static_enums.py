"""
Generate static enum constants for TypeScript SDK export.
This provides the grouped event data that homeport needs as static imports.
"""

from waldur_core.core.features import FEATURES
from waldur_core.logging.event_logger import get_event_groups
from waldur_core.permissions.enums import PermissionEnum, RoleEnum


def get_static_event_enums():
    """
    Generate static event enum constants grouped by categories.
    Returns a dictionary suitable for TypeScript constant export.
    """
    event_groups = get_event_groups()

    # Convert to format suitable for static TS constants
    static_enums = {}

    for group_key, events in event_groups.items():
        # Create constant name (e.g., 'auth' -> 'AuthEnum')
        enum_name = f"{group_key.capitalize()}Enum"

        # Create enum object mapping event names to themselves
        enum_object = {event: event for event in events}
        static_enums[enum_name] = enum_object

    return static_enums


def get_static_permission_enums():
    """
    Generate static permission enum constants.
    Returns a dictionary suitable for TypeScript constant export.
    """
    return {
        "RoleEnum": {role.name: role.value for role in RoleEnum},
        "PermissionEnum": {perm.name: perm.value for perm in PermissionEnum},
    }


def get_static_feature_enums():
    """
    Generate static feature enum constants.
    Returns a dictionary suitable for TypeScript constant export.
    """
    feature_enums = {}

    for section in FEATURES:
        section_key = section["key"]
        section_enums = {}

        for feature in section["items"]:
            enum_key = feature["key"]
            enum_value = f"{section_key}.{enum_key}"
            section_enums[enum_key] = enum_value

        # Create constant name (e.g., 'customer' -> 'CustomerFeatures')
        enum_name = f"{section_key.capitalize()}Features"
        feature_enums[enum_name] = section_enums

    return feature_enums


def generate_typescript_enums():
    """
    Generate complete TypeScript enum constants file content.
    This creates a string that can be written to a .ts file.
    """

    event_enums = get_static_event_enums()
    permission_enums = get_static_permission_enums()
    feature_enums = get_static_feature_enums()

    content = """// Auto-generated static enum constants from Waldur SDK
// This file provides all enum constants as static imports for better IDE support

// ===== EVENT ENUMS =====

"""

    # Add event enums
    for enum_name, enum_object in event_enums.items():
        content += f"export const {enum_name} = {{\n"
        for key, value in enum_object.items():
            content += f"  {key}: '{value}',\n"
        content += "} as const;\n\n"

    content += "// ===== PERMISSION ENUMS =====\n\n"

    # Add permission enums
    for enum_name, enum_object in permission_enums.items():
        content += f"export const {enum_name} = {{\n"
        for key, value in enum_object.items():
            content += f"  {key}: '{value}',\n"
        content += "} as const;\n\n"

    content += "// ===== FEATURE ENUMS =====\n\n"

    # Add feature enums
    for enum_name, enum_object in feature_enums.items():
        content += f"export const {enum_name} = {{\n"
        for key, value in enum_object.items():
            content += f"  {key}: '{value}',\n"
        content += "} as const;\n\n"

    # Add type definitions
    content += """
// ===== TYPE DEFINITIONS =====

export type EventEnumType = typeof AuthEnum | typeof ProposalEnum | typeof UsersEnum;
export type PermissionEnumType = typeof RoleEnum | typeof PermissionEnum;
export type FeatureEnumType = typeof CustomerFeatures | typeof MarketplaceFeatures;

// Helper functions
export const getEventGroupKeys = () => Object.keys({
"""

    for enum_name in event_enums.keys():
        group_key = enum_name.replace("Enum", "").lower()
        content += f"  {group_key}: {enum_name},\n"

    content += """});

export const isValidEventGroup = (group: string): boolean => {
  return getEventGroupKeys().includes(group);
};
"""

    return content
