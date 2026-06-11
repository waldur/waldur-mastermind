import ipaddress
import logging
import socket
import uuid
from datetime import UTC
from typing import cast
from urllib.parse import urlparse

import requests
from constance import config
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DateField
from django.utils import timezone
from requests.auth import HTTPBasicAuth
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.exceptions import ValidationError as RestValidationError

from waldur_auth_social.const import (
    PROVIDER_DEFAULTS,
    WRITABLE_USER_FIELDS,
    ProviderChoices,
)
from waldur_auth_social.exceptions import OAuthException
from waldur_auth_social.models import IdentityProvider
from waldur_core.core.enums import GENDER_CHOICES
from waldur_core.core.models import SshPublicKey, User
from waldur_core.core.user_attributes import (
    get_enabled_idp_sync_fields,
    get_federated_identity_sync_allowed_fields,
)
from waldur_core.core.validators import validate_ssh_public_key
from waldur_core.permissions.handlers import reactivate_user_with_logging
from waldur_core.users.enums import InvitationState
from waldur_core.users.models import GroupInvitation, Invitation

logger = logging.getLogger(__name__)

LIST_USER_FIELDS = set({"affiliations", "nationalities", "eduperson_assurance"})


def normalize_mapped_claim_value(user_field: str, value):
    """
    Normalize IdP claim value before writing it to User model field.
    """
    if isinstance(value, list | tuple):
        if user_field in LIST_USER_FIELDS:
            return list(value)

        if not value:
            return None

        if len(value) == 1:
            value = value[0]
        else:
            logger.warning(
                "Skipping multi-value claim for user field %s. Value: %s",
                user_field,
                value,
            )
            return None

    if user_field not in LIST_USER_FIELDS and isinstance(value, str):
        field = User._meta.get_field(user_field)
        if isinstance(field, DateField):
            try:
                return field.to_python(value)
            except ValidationError:
                logger.warning(
                    "Skipping claim for user field %s. Value %r is not a valid date.",
                    user_field,
                    value,
                )
                return None
        max_length = getattr(field, "max_length", None)
        if max_length and len(value) > max_length:
            logger.warning(
                "Skipping claim for user field %s. "
                "Value length %s exceeds max_length %s.",
                user_field,
                len(value),
                max_length,
            )
            return None

    return value


def get_lookup_value(
    identity_provider: IdentityProvider, backend_user: dict[str, str]
) -> str | None:
    claims = identity_provider.user_claim
    for claim in claims.split():
        claim = claim.strip()
        if claim in backend_user and claim:
            return backend_user[claim]


def get_lookup_params(
    identity_provider: IdentityProvider, backend_user: dict[str, str]
) -> dict[str, str]:
    field_name = identity_provider.user_field
    field_value = get_lookup_value(identity_provider, backend_user)

    if not field_value:
        raise OAuthException(
            identity_provider.provider,
            "Unable to match user because identity field is missing from user profile.",
        )

    return {field_name: field_value}


def parse_schac_personal_unique_id(value: str) -> str:
    """
    Parse schacPersonalUniqueID URN and extract country code + ID value.

    schacPersonalUniqueID format: urn:schac:personalUniqueID:<country-code>:<idType>:<idValue>
    Example: urn:schac:personalUniqueID:EE:EST:60001019906 -> EE60001019906

    This normalizes to the same format as TARA's sub claim (e.g., EE60001019906),
    allowing consistent storage regardless of the IdP source.
    """
    prefix = "urn:schac:personalUniqueID:"
    if not value.startswith(prefix):
        return value

    # Remove prefix and split remaining parts
    parts = value[len(prefix) :].split(":")
    if len(parts) >= 3:
        country_code = parts[0].upper()
        # idValue is the last part (in case idType contains colons)
        id_value = parts[-1]
        return f"{country_code}{id_value}"

    return value


def get_user_payload(
    identity_provider: IdentityProvider, backend_user: dict[str, str]
) -> dict[str, str]:
    # Get enabled fields for IdP sync (intersection of WRITABLE_USER_FIELDS and enabled attributes)
    enabled_sync_fields = get_enabled_idp_sync_fields()

    payload = {}
    for user_field, claims in identity_provider.attribute_mapping.items():
        # Only sync if field is in WRITABLE_USER_FIELDS AND enabled in configuration
        if user_field in WRITABLE_USER_FIELDS and user_field in enabled_sync_fields:
            for claim in claims.split():
                claim = claim.strip()
                value = normalize_mapped_claim_value(
                    user_field, backend_user.get(claim)
                )
                if user_field == "civil_number" and value:
                    value = parse_schac_personal_unique_id(value)
                if user_field == "gender" and isinstance(value, str):
                    value = value.lower()
                    valid_gender_values = {key for key, _ in GENDER_CHOICES}
                    if value not in valid_gender_values:
                        logger.warning(
                            "Skipping claim for user field gender. "
                            "Value '%s' is not one of %s.",
                            value,
                            sorted(valid_gender_values),
                        )
                        value = None
                if value:
                    payload[user_field] = value
                    break

    if identity_provider.extra_fields:
        extra_fields = {}
        for claim in identity_provider.extra_fields.split():
            claim = claim.strip()
            value = backend_user.get(claim)
            if value:
                extra_fields[claim] = value
        if extra_fields:
            payload["details"] = extra_fields

    return payload


def create_or_update_oauth_user(
    identity_provider: IdentityProvider, backend_user: dict
):
    payload = get_user_payload(identity_provider, backend_user)
    lookup_params = get_lookup_params(identity_provider, backend_user)

    roles_claim = config.WALDUR_AUTH_SOCIAL_ROLE_CLAIM
    roles = None
    if roles_claim:
        roles = backend_user.get(roles_claim)

    if roles and isinstance(roles, str):
        roles = [roles]
    elif roles is not None and not isinstance(roles, list):
        logger.warning("Roles claim %s is not a list or string: %s", roles_claim, roles)
        roles = None

    # Determine structured source for attribute tracking
    source = migrate_legacy_source(identity_provider.provider)

    user = None
    email_matched = False

    # Primary lookup
    try:
        user = cast(User, User.all_objects.get(**lookup_params))
    except User.DoesNotExist:
        # Email-based failover
        if config.OIDC_MATCHMAKING_BY_EMAIL:
            email = payload.get("email")
            if email and identity_provider.user_field != "email":
                matches = User.all_objects.filter(email__iexact=email)
                count = matches.count()
                if count > 1:
                    logger.warning(
                        "OIDC email matchmaking: %d users with email %s",
                        count,
                        email,
                    )
                    raise OAuthException(
                        identity_provider.provider,
                        "Multiple users found with the same email. "
                        "Cannot determine which account to use.",
                    )
                elif count == 1:
                    user = cast(User, matches.first())
                    email_matched = True

    if user is not None:
        # --- Existing user found (primary or email match) ---
        if not user.is_active:
            # When DEACTIVATE_USER_IF_NO_ROLES is enabled, users are auto-deactivated
            # upon losing all roles. If such a user has a pending invitation or matches
            # a group invitation, reactivate them so they can log in and accept it.
            # Without this, the user is permanently locked out: can't log in → can't
            # accept invitation → can't regain roles → stays deactivated.
            if config.DEACTIVATE_USER_IF_NO_ROLES and user.email:
                has_pending_invitation = Invitation.objects.filter(
                    email__iexact=user.email, state=InvitationState.PENDING
                ).exists()
                has_group_invitation_match = any(
                    GroupInvitation._is_pattern_match(pattern, user.email)
                    for gi in GroupInvitation.objects.filter(is_active=True).only(
                        "user_email_patterns"
                    )
                    for pattern in (gi.user_email_patterns or [])
                )
                if has_pending_invitation or has_group_invitation_match:
                    reactivate_user_with_logging(
                        user, reason="Pending invitation exists during OIDC login"
                    )
                else:
                    raise OAuthException(
                        identity_provider.provider, "User is deactivated."
                    )
            else:
                raise OAuthException(identity_provider.provider, "User is deactivated.")

        created = False
        update_fields = set()

        # If matched via email failover, update the primary lookup field
        if email_matched:
            lookup_value = get_lookup_value(identity_provider, backend_user)
            user_field = identity_provider.user_field
            old_value = getattr(user, user_field)
            setattr(user, user_field, lookup_value)
            update_fields.add(user_field)
            logger.info(
                "OIDC email matchmaking: matched user %s (pk=%s) by email %s. "
                "Updated %s from '%s' to '%s'.",
                user.username,
                user.pk,
                payload.get("email"),
                user_field,
                old_value,
                lookup_value,
            )

        # Prepare for update
        now_iso = timezone.now().isoformat()
        attribute_sources = dict(user.attribute_sources or {})

        for field, value in payload.items():
            if getattr(user, field) != value:
                setattr(user, field, value)
                update_fields.add(field)
            # Track source for all provided fields
            if value not in (None, "", []):
                attribute_sources[field] = {"source": source, "timestamp": now_iso}

        user.attribute_sources = attribute_sources
        update_fields.add("attribute_sources")

        # Ensure source is in active_isds
        active_isds = list(user.active_isds or [])
        if source not in active_isds:
            active_isds.append(source)
            user.active_isds = active_isds
            update_fields.add("active_isds")

        if roles is not None:
            should_be_staff = "staff" in roles
            if user.is_staff != should_be_staff:
                user.is_staff = should_be_staff
                update_fields.add("is_staff")

            should_be_support = "support" in roles
            if user.is_support != should_be_support:
                user.is_support = should_be_support
                update_fields.add("is_support")

        if update_fields:
            user.last_sync = timezone.now()
            update_fields.add("last_sync")
            user._change_source = source
            user.save(update_fields=update_fields)

    else:
        # --- No user found, create new ---
        if config.OIDC_BLOCK_CREATION_OF_UNINVITED_USERS:
            if "email" not in payload or not payload["email"]:
                raise OAuthException(
                    identity_provider.provider,
                    "User email is not provided. Account creation is blocked.",
                )

            if not Invitation.objects.filter(
                email__iexact=payload["email"], state=InvitationState.PENDING
            ).exists():
                email = payload["email"]
                group_invitation_match = any(
                    GroupInvitation._is_pattern_match(pattern, email)
                    for gi in GroupInvitation.objects.filter(is_active=True).only(
                        "user_email_patterns"
                    )
                    for pattern in (gi.user_email_patterns or [])
                )
                if not group_invitation_match:
                    raise OAuthException(
                        identity_provider.provider,
                        config.OIDC_BLOCK_CREATION_OF_UNINVITED_USERS_RESPONSE_MESSAGE,
                    )
        created = True

        if "username" not in payload and "username" not in lookup_params:
            payload["username"] = uuid.uuid4().hex[:30]

        merged_dict = {**lookup_params, **payload}

        if roles is not None:
            if "staff" in roles:
                merged_dict["is_staff"] = True
            if "support" in roles:
                merged_dict["is_support"] = True

        registration_method = identity_provider.provider
        if identity_provider.provider == ProviderChoices.REMOTE_EDUTEAMS:
            registration_method = ProviderChoices.EDUTEAMS
        user = cast(
            User,
            User.objects.create_user(
                registration_method=registration_method,
                **merged_dict,
            ),
        )
        user.set_unusable_password()

        # Set attribute_sources and active_isds for new user
        now_iso = timezone.now().isoformat()
        user.attribute_sources = {
            field: {"source": source, "timestamp": now_iso}
            for field, value in payload.items()
            if value not in (None, "", [])
        }
        user.active_isds = [source]
        user._change_source = source
        user.save()

    return user, created


def sync_eduteams_ssh_keys(user, backend_user, identity_provider):
    if identity_provider.provider in [
        ProviderChoices.EDUTEAMS,
        ProviderChoices.REMOTE_EDUTEAMS,
    ]:
        eduteams_keys = backend_user.get("ssh_public_key", [])
        lookup_value = get_lookup_value(identity_provider, backend_user)
        sync_user_ssh_keys(user, eduteams_keys, lookup_value)


def sync_user_ssh_keys(user, eduteams_keys, username):
    existing_keys_map = {
        key.public_key: key
        for key in SshPublicKey.objects.filter(user=user, name__startswith="eduteams_")
    }

    new_keys = set(eduteams_keys) - set(existing_keys_map.keys())
    stale_keys = set(existing_keys_map.keys()) - set(eduteams_keys)

    for key in new_keys:
        try:
            validate_ssh_public_key(key)
        except ValidationError:
            logger.debug(
                "Skipping invalid SSH key synchronization for remote eduTEAMS user %s",
                username,
            )
            continue
        name = f"eduteams_key_{uuid.uuid4().hex[:10]}"
        new_key = SshPublicKey(user=user, name=name, public_key=key)
        new_key.save()
        logger.info("%s key is added to user %s", new_key, username)

    for key in stale_keys:
        logger.info(
            "Deleting stale keys for user %s. Keys: %s",
            username,
            ", ".join([key for key in stale_keys]),
        )
        existing_keys_map[key].delete()


def pull_remote_eduteams_user(username):
    try:
        user_info = get_remote_eduteams_user_info(username)
        if "mail" in user_info and type(user_info["mail"]) is list:
            if len(user_info["mail"]) > 0:
                user_email = user_info["mail"][0]
                user_info["mail"] = user_email
            else:
                user_info["mail"] = ""
    except NotFound:
        try:
            # check across active users with default manager
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return None, False
        else:
            # Use multi-ISD aware deactivation instead of hard deactivation
            remove_user_from_isd(user, "isd:eduteams")
            return user, False
    else:
        try:
            config = IdentityProvider.objects.get(
                provider=ProviderChoices.REMOTE_EDUTEAMS
            )
        except IdentityProvider.DoesNotExist:
            config = IdentityProvider(
                provider=ProviderChoices.REMOTE_EDUTEAMS,
                **PROVIDER_DEFAULTS[ProviderChoices.REMOTE_EDUTEAMS],
            )
        user, created = create_or_update_oauth_user(config, user_info)
        sync_eduteams_ssh_keys(user, user_info, config)
    return user, created


def get_remote_eduteams_user_info(username):
    user_url = (
        f"{settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_USERINFO_URL']}/{username}"
    )
    access_token = refresh_remote_eduteams_token()
    try:
        user_response = requests.get(
            user_url, headers={"Authorization": f"Bearer {access_token}"}
        )
    except requests.exceptions.RequestException as e:
        logger.warning("Unable to get eduTEAMS user info %s", e)
        raise ParseError(f"Unable to get user info for {user_url}")

    if user_response.status_code == 404:
        raise NotFound(f"User {user_url} does not exist")

    if user_response.status_code != 200:
        raise ParseError(f"Unable to get user info for {user_url}")

    try:
        return user_response.json()
    except requests.JSONDecodeError:
        raise ParseError("Unable to parse JSON in user info response.")


def get_remote_eduteams_ssh_keys():
    ssh_api_url = settings.WALDUR_AUTH_SOCIAL.get("REMOTE_EDUTEAMS_SSH_API_URL")
    if not ssh_api_url:
        logger.warning("REMOTE_EDUTEAMS_SSH_API_URL is empty")
        return

    ssh_api_username = settings.WALDUR_AUTH_SOCIAL.get(
        "REMOTE_EDUTEAMS_SSH_API_USERNAME"
    )
    if not ssh_api_username:
        logger.warning("REMOTE_EDUTEAMS_SSH_API_USERNAME is empty")
        return

    ssh_api_password = settings.WALDUR_AUTH_SOCIAL.get(
        "REMOTE_EDUTEAMS_SSH_API_PASSWORD"
    )
    if not ssh_api_password:
        logger.warning("REMOTE_EDUTEAMS_SSH_API_PASSWORD is empty")
        return

    ssh_api_endpoint = f"{ssh_api_url}/api/vo/puhuri/ssh_keys"

    try:
        basic_auth = HTTPBasicAuth(ssh_api_username, ssh_api_password)
        response = requests.get(ssh_api_endpoint, auth=basic_auth)
    except requests.exceptions.RequestException as e:
        logger.warning("Unable to get eduTEAMS ssh keys: %s", e)
        raise ParseError(f"Unable to get eduTEAMS ssh keys for {ssh_api_endpoint}")

    if response.status_code != 200:
        raise ParseError(f"Unable to get eduTEAMS ssh keys for {ssh_api_endpoint}")

    try:
        ssh_keys = response.json()
    except requests.JSONDecodeError:
        raise ParseError("Unable to parse JSON for SSH keys")

    try:
        return ssh_keys["data"]
    except (TypeError, KeyError):
        raise ParseError("Unable to parse SSH keys")


def _get_current_refresh_token():
    """Return the current refresh token, preferring Constance over static settings."""
    token = config.REMOTE_EDUTEAMS_REFRESH_TOKEN
    if token:
        return token
    return settings.WALDUR_AUTH_SOCIAL.get("REMOTE_EDUTEAMS_REFRESH_TOKEN", "")


def refresh_remote_eduteams_token(force=False):
    if not force:
        access_token = cache.get("REMOTE_EDUTEAMS_ACCESS_TOKEN")
        if access_token:
            return access_token

    refresh_token = _get_current_refresh_token()
    if not refresh_token:
        logger.error(
            "No eduTEAMS refresh token available in Constance or Django settings."
        )
        raise RuntimeError("No refresh token available for eduTEAMS.")

    try:
        token_response = requests.post(
            settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_TOKEN_URL"],
            auth=(
                settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_CLIENT_ID"],
                settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_SECRET"],
            ),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "openid offline_access",
            },
        )
    except requests.exceptions.RequestException as e:
        logger.warning("Unable to get eduTEAMS access token. Error is %s", e)
        raise ParseError("Unable to get access token.")

    if token_response.status_code != 200:
        raise ParseError(
            f"Unable to get access token. Reason: {token_response.content}"
        )

    try:
        response_data = token_response.json()
        access_token = response_data["access_token"]
    except (requests.JSONDecodeError, TypeError, KeyError):
        raise ParseError("Unable to parse JSON in access token response.")

    new_refresh_token = response_data.get("refresh_token")
    if new_refresh_token:
        config.REMOTE_EDUTEAMS_REFRESH_TOKEN = new_refresh_token
        logger.info("eduTEAMS refresh token rotated successfully.")

    cache.set("REMOTE_EDUTEAMS_ACCESS_TOKEN", access_token, 30 * 60)
    return access_token


LEGACY_SOURCE_MAP = {
    "eduteams": "isd:eduteams",
    "remote-eduteams": "isd:eduteams",
    "tara": "isd:tara",
    "keycloak": "isd:keycloak",
}


def migrate_legacy_source(legacy_value: str) -> str:
    """Convert a legacy identity_source / registration_method to structured format."""
    if not legacy_value:
        return ""
    if ":" in legacy_value:
        return legacy_value  # Already structured
    return LEGACY_SOURCE_MAP.get(legacy_value, f"isd:{legacy_value}")


@transaction.atomic
def update_user_attributes_from_source(
    user: User,
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
    # Lock the user row for concurrent safety
    User.objects.select_for_update().filter(pk=user.pk).exists()

    now_iso = timezone.now().isoformat()
    updated_fields = set()
    attribute_sources = dict(user.attribute_sources or {})

    for field, new_value in payload.items():
        if allowed_fields and field not in allowed_fields:
            continue
        if field not in WRITABLE_USER_FIELDS:
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


def create_or_update_bridge_user(
    username: str,
    attributes: dict,
    source: str,
) -> tuple[User, bool, set[str]]:
    """
    Create or update a user via the Identity Bridge.

    Args:
        username: The CUID / username to look up.
        attributes: Dict of attribute_name -> value.
        source: ISD source identifier (e.g., "isd:puhuri").

    Returns:
        Tuple of (user, created, updated_fields).
    """
    allowed_fields = get_federated_identity_sync_allowed_fields()

    try:
        user = cast(User, User.all_objects.get(username=username))
        created = False

        if not user.is_active:
            raise ParseError(f"User {username} is deactivated.")

        updated_fields = update_user_attributes_from_source(
            user, attributes, source, allowed_fields=allowed_fields
        )
        return user, created, updated_fields

    except User.DoesNotExist:
        created = True

        # Filter attributes to allowed fields
        filtered_attrs = {
            k: v
            for k, v in attributes.items()
            if k in allowed_fields and k in WRITABLE_USER_FIELDS
        }

        now_iso = timezone.now().isoformat()
        attribute_sources = {
            field: {"source": source, "timestamp": now_iso}
            for field, value in filtered_attrs.items()
            if value not in (None, "", [])
        }

        user = cast(
            User,
            User.objects.create_user(
                username=username,
                registration_method=source,
                **filtered_attrs,
            ),
        )
        user.set_unusable_password()
        user.notifications_enabled = False
        user.attribute_sources = attribute_sources
        user.active_isds = [source]
        user._change_source = source
        user.save()

        return user, created, set(filtered_attrs.keys())


@transaction.atomic
def remove_user_from_isd(user: User, source: str) -> bool:
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
            if hasattr(user, field):
                if isinstance(getattr(user, field), list):
                    default = []
                elif field == "civil_number":
                    default = None
                else:
                    default = ""
                setattr(user, field, default)
                if field in model_fields:
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


def get_identity_bridge_stats(stale_threshold_days: int = 7) -> dict:
    """
    Compute system-wide Identity Bridge statistics.

    Returns configuration state, per-ISD user counts with staleness info,
    and total federated user counts.
    """
    from collections import Counter
    from datetime import datetime

    from waldur_core.core.user_attributes import (
        get_federated_identity_sync_allowed_fields,
    )

    now = datetime.now(UTC)

    # All users with non-empty active_isds
    federated_users = User.all_objects.exclude(active_isds=[]).exclude(
        active_isds__isnull=True
    )
    total_federated = federated_users.count()
    total_active_federated = federated_users.filter(is_active=True).count()

    # Per-ISD stats
    isd_counter = Counter()
    isd_stale_counter = Counter()
    isd_oldest_sync = {}

    for user in federated_users.only("active_isds", "attribute_sources", "is_active"):
        active_isds = user.active_isds or []
        attribute_sources = user.attribute_sources or {}

        for isd in active_isds:
            isd_counter[isd] += 1

            # Find the most recent timestamp from this ISD
            latest_ts = None
            for _field, info in attribute_sources.items():
                if not isinstance(info, dict):
                    continue
                if info.get("source") != isd:
                    continue
                ts_str = info.get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    if latest_ts is None or ts > latest_ts:
                        latest_ts = ts
                except (ValueError, TypeError):
                    continue

            if latest_ts is None:
                isd_stale_counter[isd] += 1
            else:
                age_days = (now - latest_ts).total_seconds() / 86400
                if age_days > stale_threshold_days:
                    isd_stale_counter[isd] += 1

                # Track oldest sync per ISD
                if isd not in isd_oldest_sync or latest_ts < isd_oldest_sync[isd]:
                    isd_oldest_sync[isd] = latest_ts

    users_per_isd = sorted(
        [
            {
                "isd": isd,
                "user_count": count,
                "stale_user_count": isd_stale_counter.get(isd, 0),
                "oldest_sync": isd_oldest_sync[isd].isoformat()
                if isd in isd_oldest_sync
                else None,
            }
            for isd, count in isd_counter.items()
        ],
        key=lambda x: x["user_count"],
        reverse=True,
    )

    # Identity managers
    identity_managers = []
    for mgr in (
        User.objects.filter(is_identity_manager=True)
        .only("uuid", "first_name", "last_name", "username", "managed_isds")
        .order_by("first_name", "last_name")
    ):
        identity_managers.append(
            {
                "uuid": str(mgr.uuid),
                "full_name": mgr.full_name or mgr.username,
                "managed_isds": mgr.managed_isds or [],
            }
        )

    return {
        "enabled": config.FEDERATED_IDENTITY_SYNC_ENABLED,
        "deactivation_policy": config.FEDERATED_IDENTITY_DEACTIVATION_POLICY,
        "allowed_attributes": sorted(get_federated_identity_sync_allowed_fields()),
        "total_federated_users": total_federated,
        "total_active_federated_users": total_active_federated,
        "users_per_isd": users_per_isd,
        "stale_threshold_days": stale_threshold_days,
        "identity_managers": identity_managers,
    }


def validate_and_get_redirect_url(
    identity_provider: IdentityProvider,
    referrer: str | None,
    return_url: str | None = None,
) -> str:
    """
    Validates the return URL or referrer against allowed redirects and returns
    the appropriate redirect URL for redirecting after OIDC authentication.

    Args:
        identity_provider: The IdentityProvider instance
        referrer: The referrer URL from the request headers
        return_url: The explicit return_url from query parameter (takes priority)

    Returns:
        str: The validated redirect URL to redirect to

    Raises:
        OAuthException: If the return_url/referrer is not in the allowed list
    """
    # Prioritize explicit return_url over referrer header
    source_url = return_url or referrer

    # If no allowed_redirects configured, fall back to HOMEPORT_URL
    if not identity_provider.allowed_redirects:
        return config.HOMEPORT_URL

    # If no source URL provided, use the first allowed redirect
    if not source_url:
        return identity_provider.allowed_redirects[0]

    # Parse the source URL to extract the base URL (scheme + netloc)
    try:
        parsed_url = urlparse(source_url)

        # Validate scheme (only http/https allowed)
        if parsed_url.scheme.lower() not in ("http", "https"):
            raise OAuthException(
                identity_provider.provider,
                "Invalid URL scheme. Only http and https are allowed.",
            )

        # Validate netloc is not empty
        if not parsed_url.netloc:
            raise OAuthException(
                identity_provider.provider,
                "Invalid return URL format. Missing domain.",
            )

        # Normalize: lowercase scheme and netloc for case-insensitive matching
        url_base = f"{parsed_url.scheme.lower()}://{parsed_url.netloc.lower()}"
    except OAuthException:
        raise
    except Exception as e:
        logger.warning("Failed to parse source URL %s: %s", source_url, e)
        raise OAuthException(
            identity_provider.provider,
            "Invalid return URL format.",
        )

    # Check if source URL base is in the allowed list (exact matching)
    if url_base not in identity_provider.allowed_redirects:
        logger.warning(
            "Source URL %s not in allowed redirects %s",
            url_base,
            identity_provider.allowed_redirects,
        )
        raise OAuthException(
            identity_provider.provider,
            f"Return URL domain {url_base} is not in the allowed redirects list.",
        )

    # Return the base URL without trailing slash (consistent with allowlist normalization)
    return url_base


def validate_safe_remote_url(url: str, field: str = "discovery_url") -> None:
    """
    Guard server-side fetches (e.g. OIDC discovery) against SSRF.

    Resolves the URL host and rejects any URL whose host resolves to a
    loopback, link-local, reserved, multicast or unspecified address. Only
    http/https URLs with an explicit host are allowed.

    Note that RFC-1918 private addresses are intentionally *allowed*: the OIDC
    IdP (e.g. Keycloak) is commonly an in-cluster service reached via a
    ClusterIP (10.x / 192.168.x), and that is a legitimate configuration on the
    same trust boundary as the mastermind pod. What we block is what crosses a
    trust boundary even for staff: link-local (this covers the 169.254.169.254
    cloud metadata endpoint, which would leak the pod's cloud IAM credentials)
    and loopback (services bound to localhost on the pod itself).

    Raises rest_framework ValidationError keyed on ``field`` so callers can
    surface a clean 400 instead of leaking the fetch result.

    Residual risk: this resolves the host once, so a hostile DNS server could
    still rebind to a blocked address between this check and the actual request
    (TOCTOU). Full protection would require pinning the connection to the
    validated address. For the staff-only admin surface this guards, the
    resolve-and-block check is the pragmatic mitigation.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise RestValidationError({field: "Only http and https URLs are allowed."})

    hostname = parsed.hostname
    if not hostname:
        raise RestValidationError({field: "URL must include a host."})

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        addrinfos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise RestValidationError({field: f"Unable to resolve host {hostname!r}."})

    for info in addrinfos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise RestValidationError(
                {field: f"Unable to parse resolved address for host {hostname!r}."}
            )
        # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) before classifying.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        # NB: is_private is deliberately omitted — in-cluster IdPs live on
        # RFC-1918 addresses. Block only what crosses a trust boundary.
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise RestValidationError(
                {field: f"URL host resolves to a disallowed address ({ip})."}
            )
