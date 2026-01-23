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
        "country_of_residence",
        "nationality",
        "nationalities",
        "organization_country",
        "organization_type",
        "eduperson_assurance",
        "civil_number",
        "identity_source",
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
