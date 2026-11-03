"""Access tokens FirecREST accepts on behalf of a Waldur user.

FirecREST validates a bearer token against the identity provider's keys and
derives the POSIX user from one of its claims, so Waldur can only call it as
the user if it holds a token the same identity provider issued to that user.
Waldur stores the tokens of users who signed in through OIDC
(``OAuthToken``); this module hands out a valid access token from it,
refreshing it with the stored refresh token when it is about to expire.
"""

import logging
import time

import jwt
import requests
from django.db import transaction

from waldur_auth_social.models import IdentityProvider, OAuthToken

logger = logging.getLogger(__name__)

# Refresh a little before the expiry so the token is still valid when
# FirecREST checks it at the end of a slow request.
EXPIRY_LEEWAY_SECONDS = 30


class TokenUnavailable(Exception):
    """The user has no usable identity provider token for FirecREST.

    ``sign_in_provider`` names the identity provider a fresh sign-in through
    would fix it with (the stored token's), so the UI can offer that sign-in.
    """

    def __init__(self, message, sign_in_provider=None):
        super().__init__(message)
        self.sign_in_provider = sign_in_provider


SESSION_EXPIRED = (
    "Your identity provider session has expired. Sign in again to use HPC services."
)


def _expires_at(access_token):
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        # Opaque token: nothing to check locally, FirecREST decides.
        return None
    return claims.get("exp")


def _is_fresh(access_token):
    expires_at = _expires_at(access_token)
    return expires_at is None or expires_at - EXPIRY_LEEWAY_SECONDS > time.time()


def _refresh(oauth_token: OAuthToken):
    if not oauth_token.refresh_token:
        raise TokenUnavailable(SESSION_EXPIRED, oauth_token.provider)
    try:
        provider = IdentityProvider.objects.get(
            provider=oauth_token.provider, is_active=True
        )
    except IdentityProvider.DoesNotExist:
        raise TokenUnavailable(
            f"Identity provider {oauth_token.provider} is not configured."
        )
    try:
        response = requests.post(
            provider.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": oauth_token.refresh_token,
                "client_id": provider.client_id,
                "client_secret": provider.client_secret,
            },
            verify=provider.verify_ssl,
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise TokenUnavailable(f"Unable to reach identity provider: {e}")
    if not response.ok:
        logger.info(
            "Refreshing %s token of user %s failed with %s",
            oauth_token.provider,
            oauth_token.user_id,
            response.status_code,
        )
        raise TokenUnavailable(SESSION_EXPIRED, oauth_token.provider)
    data = response.json()
    oauth_token.access_token = data["access_token"]
    # Providers that rotate refresh tokens invalidate the old one on use.
    oauth_token.refresh_token = data.get("refresh_token", oauth_token.refresh_token)
    oauth_token.save(update_fields=["access_token", "refresh_token"])


def get_sign_in_provider(user) -> str | None:
    """The identity provider that issued *user*'s stored token, if any."""
    return (
        OAuthToken.objects.filter(user=user).values_list("provider", flat=True).first()
    )


def get_access_token(user) -> str:
    """Return a valid access token of *user*, refreshing it if needed."""
    try:
        oauth_token = OAuthToken.objects.get(user=user)
    except OAuthToken.DoesNotExist:
        raise TokenUnavailable(
            "HPC services need a sign-in through your identity provider. "
            "Sign out and sign in again with your institutional account."
        )
    if oauth_token.access_token and _is_fresh(oauth_token.access_token):
        return oauth_token.access_token

    # Lock the row: with rotating refresh tokens two concurrent refreshes
    # would have the second one fail with the already-used token.
    with transaction.atomic():
        oauth_token = OAuthToken.objects.select_for_update().get(pk=oauth_token.pk)
        if not (oauth_token.access_token and _is_fresh(oauth_token.access_token)):
            _refresh(oauth_token)
    return oauth_token.access_token
