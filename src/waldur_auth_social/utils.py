import logging
import uuid
from typing import cast

import requests
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
from waldur_core.core.validators import validate_ssh_public_key

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


def get_user_payload(
    identity_provider: IdentityProvider, backend_user: dict[str, str]
) -> dict[str, str]:
    payload = {}
    for user_field, claims in identity_provider.attribute_mapping.items():
        if user_field in WRITABLE_USER_FIELDS:
            for claim in claims.split():
                claim = claim.strip()
                value = backend_user.get(claim)
                if user_field == "email" and isinstance(value, list):
                    value = value[0]
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

    if "username" not in payload and "username" not in lookup_params:
        payload["username"] = uuid.uuid4().hex[:30]

    try:
        created = False
        # Use all_objects to reactivate a user who might have been deactivated
        user = cast(User, User.all_objects.get(**lookup_params))

        # Prepare for update
        update_fields = set()
        if not user.is_active:
            user.is_active = True
            update_fields.add("is_active")
        user.last_sync = timezone.now()
        update_fields.add("last_sync")

        for field, value in payload.items():
            if getattr(user, field) != value:
                setattr(user, field, value)
                update_fields.add(field)

        if update_fields:
            user.save(update_fields=update_fields)

    except User.DoesNotExist:
        created = True
        user = cast(
            User,
            User.objects.create_user(
                registration_method=identity_provider.provider,
                **lookup_params,
                **payload,
            ),
        )
        user.set_unusable_password()
        user.save()

    if identity_provider.provider == ProviderChoices.EDUTEAMS:
        eduteams_keys = backend_user.get("ssh_public_key", [])
        lookup_value = get_lookup_value(identity_provider, backend_user)
        sync_user_ssh_keys(user, eduteams_keys, lookup_value)
        if user.notifications_enabled:
            user.notifications_enabled = False
            user.save(update_fields=["notifications_enabled"])

    return user, created


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
    except NotFound:
        try:
            # check across active users with default manager
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return
        else:
            user.is_active = False
            user.last_sync = timezone.now()
            user.save(update_fields=["is_active", "last_sync"])
    else:
        try:
            config = IdentityProvider.objects.get(provider=ProviderChoices.EDUTEAMS)
        except IdentityProvider.DoesNotExist:
            config = IdentityProvider(
                provider=ProviderChoices.EDUTEAMS,
                **PROVIDER_DEFAULTS[ProviderChoices.EDUTEAMS],
            )
        user, _ = create_or_update_oauth_user(config, user_info)
    return user


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


def refresh_remote_eduteams_token():
    access_token = cache.get("REMOTE_EDUTEAMS_ACCESS_TOKEN")
    if access_token:
        return access_token
    try:
        token_response = requests.post(
            settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_TOKEN_URL"],
            auth=(
                settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_CLIENT_ID"],
                settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_SECRET"],
            ),
            data={
                "grant_type": "refresh_token",
                "refresh_token": settings.WALDUR_AUTH_SOCIAL[
                    "REMOTE_EDUTEAMS_REFRESH_TOKEN"
                ],
                "scope": "openid",
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
        access_token = token_response.json()["access_token"]
    except (requests.JSONDecodeError, TypeError, KeyError):
        raise ParseError("Unable to parse JSON in access token response.")

    cache.set("REMOTE_EDUTEAMS_ACCESS_TOKEN", access_token, 30 * 60)
    return access_token
