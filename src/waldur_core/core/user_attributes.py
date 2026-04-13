"""
User profile attribute configuration utilities.

This module provides a centralized way to manage which user profile attributes
are enabled system-wide. It controls:
1. Which attributes are synced from IdPs during OIDC authentication
2. Which attributes are available in the UI

Architecture:
- WRITABLE_USER_FIELDS (in waldur_auth_social.const) is a security whitelist
  defining what CAN be written to user profiles
- ENABLED_USER_PROFILE_ATTRIBUTES (Constance setting) is admin configuration
  defining what SHOULD be written
- Effective fields = intersection of both
"""

from constance import config

from waldur_auth_social.const import WRITABLE_USER_FIELDS

# Core attributes that are always enabled and cannot be disabled
CORE_USER_ATTRIBUTES = frozenset(
    [
        "username",
        "email",
        "first_name",
        "last_name",
        "full_name",
    ]
)

# All configurable profile attributes that can be enabled/disabled
ALL_PROFILE_ATTRIBUTES = frozenset(
    [
        "phone_number",
        "organization",
        "job_title",
        "affiliations",
        "gender",
        "personal_title",
        "birth_date",
        "place_of_birth",
        "address",
        "country_of_residence",
        "nationality",
        "nationalities",
        "organization_country",
        "organization_type",
        "organization_registry_code",
        "eduperson_assurance",
        "civil_number",
        "identity_source",
        "active_isds",
    ]
)


def get_enabled_profile_attributes() -> set[str]:
    """
    Get enabled profile attributes from Constance + core attributes.

    Returns the union of:
    - Core attributes (always enabled)
    - Profile attributes from ENABLED_USER_PROFILE_ATTRIBUTES setting
      that are valid (exist in ALL_PROFILE_ATTRIBUTES)
    """
    enabled_list = config.ENABLED_USER_PROFILE_ATTRIBUTES or []
    # Only include valid profile attributes
    enabled_profile = set(enabled_list) & ALL_PROFILE_ATTRIBUTES
    return CORE_USER_ATTRIBUTES | enabled_profile


def get_enabled_idp_sync_fields() -> set[str]:
    """
    Get fields to sync from IdP (intersection with WRITABLE_USER_FIELDS).

    This returns fields that are both:
    - Enabled in ENABLED_USER_PROFILE_ATTRIBUTES (or core attributes)
    - Listed in WRITABLE_USER_FIELDS security whitelist
    """
    return get_enabled_profile_attributes() & set(WRITABLE_USER_FIELDS)


def is_attribute_enabled(attribute_name: str) -> bool:
    """
    Check if a specific attribute is enabled.

    Args:
        attribute_name: Name of the attribute to check

    Returns:
        True if the attribute is enabled (either core or configured)
    """
    return attribute_name in get_enabled_profile_attributes()


def get_federated_identity_sync_allowed_fields() -> set[str]:
    """
    Get fields allowed for the Identity Bridge (three-way intersection).

    Returns the intersection of:
    - FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES (Constance setting)
    - WRITABLE_USER_FIELDS (security whitelist)
    - Enabled profile attributes (ENABLED_USER_PROFILE_ATTRIBUTES + core)
    """
    bridge_list = config.FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES or []
    return (
        set(bridge_list) & set(WRITABLE_USER_FIELDS) & get_enabled_profile_attributes()
    )


def get_mandatory_attributes() -> list[str]:
    """
    Get the list of mandatory user attributes from Constance.

    Returns:
        List of attribute names that are configured as mandatory.
    """
    return config.MANDATORY_USER_ATTRIBUTES or []


def get_user_missing_mandatory_attributes(user) -> list[str]:
    """
    Check which mandatory attributes are missing for a user.

    Args:
        user: User instance to check

    Returns:
        List of attribute names that are mandatory but missing/empty
    """
    mandatory = get_mandatory_attributes()
    missing = []

    for attr in mandatory:
        value = getattr(user, attr, None)
        # Consider empty strings, None, and empty lists as missing
        if value is None or value == "" or value == []:
            missing.append(attr)

    return missing


def is_user_profile_complete(user) -> bool:
    """
    Check if a user has completed all mandatory profile fields.

    Args:
        user: User instance to check

    Returns:
        True if all mandatory fields are filled, False otherwise
    """
    return len(get_user_missing_mandatory_attributes(user)) == 0


def get_profile_completeness_details(user) -> dict:
    """
    Get full details about a user's profile completeness.

    Args:
        user: User instance to check

    Returns:
        Dictionary containing:
        - is_complete: bool - whether all mandatory fields are filled
        - missing_fields: list - fields that are mandatory but missing
        - mandatory_fields: list - all mandatory fields
        - enforcement_enabled: bool - whether enforcement is active
    """
    mandatory = get_mandatory_attributes()
    missing = get_user_missing_mandatory_attributes(user)

    return {
        "is_complete": len(missing) == 0,
        "missing_fields": missing,
        "mandatory_fields": mandatory,
        "enforcement_enabled": config.ENFORCE_MANDATORY_USER_ATTRIBUTES,
    }
