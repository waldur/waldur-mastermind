"""
User profile attribute configuration utilities.

This module provides a centralized way to manage which user profile attributes
are enabled system-wide. It controls:
1. Which attributes are synced from IdPs during OIDC authentication
2. Which attributes are available in the UI

Architecture:
- WRITABLE_USER_FIELDS is a security whitelist defining what CAN be written to
  user profiles
- ENABLED_USER_PROFILE_ATTRIBUTES (Constance setting) is admin configuration
  defining what SHOULD be written
- Effective fields = intersection of both
"""

from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone

from waldur_core.configuration import config

# Security whitelist of user model fields that external IdPs are allowed to write.
WRITABLE_USER_FIELDS = (
    "first_name",
    "last_name",
    "identity_source",
    "organization",
    "affiliations",
    "civil_number",
    "email",
    "phone_number",
    "birth_date",
    "gender",
    "personal_title",
    "place_of_birth",
    "address",
    "country_of_residence",
    "nationality",
    "nationalities",
    "organization_country",
    "organization_type",
    "organization_registry_code",
    "organization_vat_code",
    "organization_address",
    "eduperson_assurance",
    "uid_number",
    "primary_gid",
)

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
        "organization_vat_code",
        "organization_address",
        "eduperson_assurance",
        "civil_number",
        "identity_source",
        "active_isds",
        "uid_number",
        "primary_gid",
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


def _field_locked(user, field: str, source: str) -> bool:
    """Whether ``source`` may not write ``field`` of ``user``.

    Fields in FEDERATED_IDENTITY_LOCKED_FIELDS belong to the authoritative
    ISD once it has asserted the user; other sources cannot change them.
    """
    authoritative_isd = config.FEDERATED_IDENTITY_AUTHORITATIVE_ISD
    locked_fields = config.FEDERATED_IDENTITY_LOCKED_FIELDS or []
    return bool(
        authoritative_isd
        and field in locked_fields
        and source != authoritative_isd
        and authoritative_isd in (user.active_isds or [])
    )


@transaction.atomic
def update_user_attributes_from_source(
    user,
    payload: dict,
    source: str,
    *,
    allowed_fields: set[str] | None = None,
) -> set[str]:
    """
    Update user attributes with source-aware ownership tracking.

    Implements the preserve-other-sources policy:
    - If a field's new value is empty and the current owner is a different source,
      the field is preserved (not cleared).
    - If the new value is non-empty, the field is updated and ownership transfers.
    - Timestamps are updated even if the value is unchanged (confirms freshness).

    Args:
        user: User instance (must be fetched with select_for_update for concurrency).
        payload: Dict of field_name -> new_value.
        source: Source identifier (e.g., "isd:puhuri").
        allowed_fields: Optional set of fields to restrict updates to.

    Returns:
        Set of field names that were actually changed.
    """
    User = get_user_model()
    # Lock the user row for concurrent safety, and compare and write from the
    # row as it is now. A copy read before the lock, held across a pull's HTTP
    # call or a bulk run, would compare the payload with an old email and still
    # replace its source, dropping the mark of a self-set one
    # (User.find_by_email) while the address stays.
    User.objects.select_for_update().filter(pk=user.pk).exists()
    user.refresh_from_db(
        fields=[*WRITABLE_USER_FIELDS, "attribute_sources", "active_isds"]
    )

    now_iso = timezone.now().isoformat()
    updated_fields = set()
    attribute_sources = dict(user.attribute_sources or {})

    # Fields locked to an authoritative ISD (e.g. EFP/passport) must not be
    # overwritten by other sources pushing over the Identity Bridge, mirroring
    # the guard in create_or_update_oauth_user. See FEDERATED_IDENTITY_*.
    for field, new_value in payload.items():
        if allowed_fields and field not in allowed_fields:
            continue
        if field not in WRITABLE_USER_FIELDS:
            continue
        if _field_locked(user, field, source):
            # Preserve the authoritative ISD's value; do not update or transfer
            # ownership for locked fields.
            continue

        current_value = getattr(user, field, None)
        current_source_info = attribute_sources.get(field, {})
        current_owner = (
            current_source_info.get("source")
            if isinstance(current_source_info, dict)
            else current_source_info
        )

        # Determine if new value is "empty"
        is_empty = new_value is None or new_value == "" or new_value == []

        if is_empty and current_owner and current_owner != source:
            # Preserve-other-sources: skip clearing fields owned by another source
            continue

        if current_value != new_value:
            setattr(user, field, new_value)
            updated_fields.add(field)

        # Always update source/timestamp for freshness tracking
        attribute_sources[field] = {"source": source, "timestamp": now_iso}

    user.attribute_sources = attribute_sources

    # Ensure source is in active_isds
    active_isds = list(user.active_isds or [])
    if source not in active_isds:
        active_isds.append(source)
        user.active_isds = active_isds
        updated_fields.add("active_isds")

    # Set _change_source for audit trail
    user._change_source = source

    if updated_fields or attribute_sources != (user.attribute_sources or {}):
        save_fields = updated_fields | {"attribute_sources", "active_isds", "last_sync"}
        user.last_sync = timezone.now()
        user.save(update_fields=save_fields)

    return updated_fields


@transaction.atomic
def remove_user_from_isd(user, source: str) -> bool:
    """
    Remove a user from an ISD and handle deactivation policy.

    - Removes source from active_isds.
    - Clears attribute_sources entries owned by this source.
    - Clears corresponding attribute values.
    - Deactivates user if active_isds is empty and policy is 'all_isds_removed',
      or always if policy is 'any_isd_removed'.

    Args:
        user: User instance.
        source: ISD source identifier to remove.

    Returns:
        True if user was deactivated, False otherwise.
    """
    User = get_user_model()
    User.objects.select_for_update().filter(pk=user.pk).exists()

    active_isds = list(user.active_isds or [])
    if source in active_isds:
        active_isds.remove(source)
        user.active_isds = active_isds

    # Clear attribute_sources and values owned by this source
    attribute_sources = dict(user.attribute_sources or {})
    model_fields = {f.name for f in User._meta.concrete_fields}
    cleared_fields = []
    for field, source_info in list(attribute_sources.items()):
        owner = (
            source_info.get("source") if isinstance(source_info, dict) else source_info
        )
        if owner == source:
            del attribute_sources[field]
            # Clear the actual field value. civil_number must clear to None:
            # it has a unique constraint and relies on NULL for absent values,
            # so "" would collide as soon as two users are cleared.
            if hasattr(user, field) and field in model_fields:
                field_obj = User._meta.get_field(field)
                if isinstance(field_obj, models.JSONField):
                    default = field_obj.get_default()
                elif isinstance(getattr(user, field), list):
                    default = []
                elif field == "civil_number":
                    default = None
                else:
                    default = ""
                setattr(user, field, default)
                cleared_fields.append(field)

    user.attribute_sources = attribute_sources

    policy = config.FEDERATED_IDENTITY_DEACTIVATION_POLICY
    should_deactivate = False

    if policy == "any_isd_removed":
        should_deactivate = True
    elif not active_isds:
        # all_isds_removed (default): deactivate only when empty
        should_deactivate = True

    if should_deactivate:
        user.is_active = False
        user.deactivation_reason = (
            f"Identity source '{source}' removed (policy: {policy})"
        )

    user._change_source = source
    user.last_sync = timezone.now()
    user.save(
        update_fields=[
            "active_isds",
            "attribute_sources",
            "is_active",
            "deactivation_reason",
            "last_sync",
            # Without these the cleared values above never reach the DB.
            *cleared_fields,
        ]
    )

    return should_deactivate
