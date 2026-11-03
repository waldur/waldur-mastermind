"""
Revoking the identity provider token Waldur keeps for a user.

A sign-in through an OIDC provider stores the provider's refresh token in
``OAuthToken``, encrypted, so Waldur can call services as the user later (the
HPC workspace calls FirecREST with access tokens minted from it, see
``waldur_auth_social.oauth_tokens``). Ending the provider's browser session at
logout does not invalidate an offline refresh token, so the stored token is
revoked at the provider (RFC 7009) and removed when the user logs out, is
deactivated or is deleted, after ``OIDC_TOKEN_MAX_IDLE_DAYS`` without a
sign-in, while the HPC workspace is off, and when a sign-in in another session
replaces it. A token the provider refuses to refresh is removed too.

Removal always happens. Revocation is best effort: a provider without a
revocation endpoint, or one that cannot be reached, is logged and skipped, so
it never blocks a logout.
"""

import hashlib
import logging

import requests
from django.core.cache import cache
from django.db import transaction

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType

from .models import IdentityProvider, OAuthToken
from .token_cache import forget_access_tokens

logger = logging.getLogger(__name__)

# Logout waits for the revocation call, so keep it short.
REQUEST_TIMEOUT = 5

# The revocation endpoint rarely changes; a provider without one is cached too
# (as an empty string), so it is not looked up again on every logout.
ENDPOINT_CACHE_TIMEOUT = 60 * 60


class RevocationReason:
    LOGOUT = "logout"
    DEACTIVATED = "deactivated"
    DELETED = "deleted"
    IDLE = "idle"
    DISABLED = "disabled"
    REPLACED = "replaced"
    REJECTED = "rejected"


def _endpoint_cache_key(idp: IdentityProvider) -> str:
    digest = hashlib.sha256(idp.discovery_url.encode()).hexdigest()
    return f"oidc_revocation_endpoint:{idp.provider}:{digest}"


def get_revocation_endpoint(idp: IdentityProvider) -> str | None:
    """The provider's ``revocation_endpoint`` from its discovery document."""
    key = _endpoint_cache_key(idp)
    endpoint = cache.get(key)
    if endpoint is None:
        try:
            response = requests.get(
                idp.discovery_url, verify=idp.verify_ssl, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            endpoint = response.json().get("revocation_endpoint") or ""
        except (requests.RequestException, ValueError, AttributeError) as e:
            # Not cached: the next logout tries the discovery document again.
            logger.warning(
                "Unable to read the revocation endpoint of identity provider %s: %s",
                idp.provider,
                e,
            )
            return None
        cache.set(key, endpoint, ENDPOINT_CACHE_TIMEOUT)
    return endpoint or None


def revoke_at_provider(provider: str, refresh_token: str) -> bool:
    """Ask the provider to revoke *refresh_token*. Returns whether it confirmed.

    Revoking the refresh token ends the provider session it belongs to, and
    with it the access tokens issued from it.
    """
    try:
        idp = IdentityProvider.objects.get(provider=provider)
    except IdentityProvider.DoesNotExist:
        logger.info("Identity provider %s is gone; token not revoked.", provider)
        return False
    endpoint = get_revocation_endpoint(idp)
    if not endpoint:
        logger.info(
            "Identity provider %s publishes no revocation endpoint; token not revoked.",
            provider,
        )
        return False
    try:
        response = requests.post(
            endpoint,
            data={
                "token": refresh_token,
                "token_type_hint": "refresh_token",
                "client_id": idp.client_id,
                "client_secret": idp.client_secret,
            },
            verify=idp.verify_ssl,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.warning("Unable to revoke a %s token: %s", provider, e)
        return False
    # RFC 7009: 200 whether or not the token was still valid.
    if not response.ok:
        logger.warning(
            "Identity provider %s refused to revoke a token with %s",
            provider,
            response.status_code,
        )
        return False
    return True


def _emit_revoked(user, provider, reason):
    event_logger.emit(
        "Stored identity provider token of {affected_user_username} was "
        "revoked ({reason}).",
        event_type=EventType.OIDC_TOKEN_REVOKED,
        event_context={"affected_user": user, "provider": provider, "reason": reason},
        scopes=[user],
    )


def _revoke_after_commit(provider, refresh_token):
    """Call the provider once the transaction commits, so a rolled-back change
    keeps the token. The token only travels in this process's memory, never
    through the task broker."""
    transaction.on_commit(lambda: revoke_at_provider(provider, refresh_token))


def forget_oauth_tokens(user, reason: str, notify_provider: bool = True) -> None:
    """Remove the user's stored identity provider token and revoke it.

    The row is deleted at once, in the caller's transaction, and the access
    tokens minted from it leave the cache. *notify_provider* is False for a
    token the provider has already refused.
    """
    with transaction.atomic():
        # Waits for a refresh in progress, so the token it rotates to is the
        # one revoked, and the access token it caches is dropped below.
        stored = list(
            OAuthToken.objects.select_for_update()
            .filter(user=user)
            .values_list("provider", "refresh_token")
        )
        if stored:
            OAuthToken.objects.filter(user=user).delete()
    forget_access_tokens(user.pk)
    for provider, refresh_token in stored:
        if notify_provider and refresh_token:
            _revoke_after_commit(provider, refresh_token)
        _emit_revoked(user, provider, reason)


def revoke_replaced_token(user, provider, refresh_token) -> None:
    """Revoke a token a new sign-in has replaced; the caller removed it."""
    if refresh_token:
        _revoke_after_commit(provider, refresh_token)
    _emit_revoked(user, provider, RevocationReason.REPLACED)
