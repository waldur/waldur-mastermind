"""
Revoking the identity provider tokens Waldur keeps for a user.

A sign-in through an OIDC provider stores the provider's access and refresh
tokens in ``OAuthToken`` so Waldur can call services as the user later (the
HPC workspace calls FirecREST with them). Ending the provider's browser session
at logout does not invalidate an offline refresh token, so when the user logs
out, is deactivated or is deleted, the stored token is revoked at the
provider (RFC 7009) and removed.

Removal always happens. Revocation is best effort: a provider without a
revocation endpoint, or one that cannot be reached, is logged and skipped, so
it never blocks a logout.
"""

import hashlib
import logging

import requests
from django.core.cache import cache
from django.db import transaction

from waldur_auth_social.models import IdentityProvider, OAuthToken

logger = logging.getLogger(__name__)

# Logout waits for the revocation call, so keep it short.
REQUEST_TIMEOUT = 5

# The revocation endpoint rarely changes; a provider without one is cached too
# (as an empty string), so it is not looked up again on every logout.
ENDPOINT_CACHE_TIMEOUT = 60 * 60


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


def revoke_at_provider(provider: str, token: str, token_type_hint: str) -> bool:
    """Ask the provider to revoke *token*. Returns whether it confirmed."""
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
                "token": token,
                "token_type_hint": token_type_hint,
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


def forget_oauth_tokens(user) -> None:
    """Remove the user's stored identity provider tokens and revoke them.

    The rows are deleted at once, in the caller's transaction; the provider is
    called once it commits, so a rolled-back deactivation keeps the token.
    """
    stored = list(
        OAuthToken.objects.filter(user=user).values_list(
            "provider", "access_token", "refresh_token"
        )
    )
    if not stored:
        return
    OAuthToken.objects.filter(user=user).delete()
    for provider, access_token, refresh_token in stored:
        # Revoking the refresh token ends the provider session it belongs to,
        # and with it the access tokens issued from it.
        if refresh_token:
            token, hint = refresh_token, "refresh_token"
        elif access_token:
            token, hint = access_token, "access_token"
        else:
            continue
        transaction.on_commit(
            lambda provider=provider, token=token, hint=hint: revoke_at_provider(
                provider, token, hint
            )
        )
