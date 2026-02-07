import logging
import uuid
from typing import cast
from urllib.parse import urlparse

import requests
from constance import config
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone
from requests.auth import HTTPBasicAuth
from rest_framework.exceptions import NotFound, ParseError

from waldur_auth_social.const import (
    PROVIDER_DEFAULTS,
    WRITABLE_USER_FIELDS,
    ProviderChoices,
)
from waldur_auth_social.exceptions import OAuthException
from waldur_auth_social.models import IdentityProvider
from waldur_core.core.models import SshPublicKey, User
from waldur_core.core.user_attributes import get_enabled_idp_sync_fields
from waldur_core.core.validators import validate_ssh_public_key
from waldur_core.users.enums import InvitationState
from waldur_core.users.models import Invitation

logger = logging.getLogger(__name__)


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
                value = backend_user.get(claim)
                if user_field == "email" and isinstance(value, list):
                    value = value[0]
                if user_field == "civil_number" and value:
                    value = parse_schac_personal_unique_id(value)
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

    try:
        created = False
        # Use all_objects to reactivate a user who might have been deactivated
        user = cast(User, User.all_objects.get(**lookup_params))

        if not user.is_active:
            raise OAuthException(identity_provider.provider, "User is deactivated.")

        # Prepare for update
        update_fields = set()

        for field, value in payload.items():
            if getattr(user, field) != value:
                setattr(user, field, value)
                update_fields.add(field)

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
            user.save(update_fields=update_fields)

    except User.DoesNotExist:
        if config.OIDC_BLOCK_CREATION_OF_UNINVITED_USERS:
            if "email" not in payload or not payload["email"]:
                raise OAuthException(
                    identity_provider.provider,
                    "User email is not provided. Account creation is blocked.",
                )

            if not Invitation.objects.filter(
                email__iexact=payload["email"], state=InvitationState.PENDING
            ).exists():
                raise OAuthException(
                    identity_provider.provider,
                    "Account creation is blocked for uninvited users.",
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
        logger.info("%s key is added to user %s", new_key)

    for key in stale_keys:
        logger.info(
            "Deleting stale keys for user %s. Keys: ",
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
            user.is_active = False
            user.last_sync = timezone.now()
            user.save(update_fields=["is_active", "last_sync"])
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
