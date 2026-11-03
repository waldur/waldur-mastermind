"""Identity provider tokens Waldur keeps to act as a user.

Services such as FirecREST accept only a token the identity provider issued to
the user, so Waldur keeps the refresh token of a user's last OIDC sign-in and
mints access tokens from it. A refresh token is a live credential, so:

- it is kept only while a service that needs it is enabled (the HPC workspace,
  ``FIRECREST_ALLOWED_API_URLS``), and encrypted at rest;
- access tokens never reach the database: they stay in the cache, encrypted,
  until shortly before they expire (``waldur_auth_social.token_cache``);
- it is deleted, and revoked at the identity provider, on logout, on
  deactivation and deletion, after ``OIDC_TOKEN_MAX_IDLE_DAYS`` without a
  sign-in, while the HPC workspace is off, when a sign-in in another session
  replaces it, and once the identity provider rejects it
  (``waldur_auth_social.revocation``).
"""

import logging
from datetime import timedelta

import jwt
import requests
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from waldur_core.configuration import config
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType

from . import revocation
from .models import IdentityProvider, OAuthToken
from .token_cache import cache_access_token, cached_access_token, forget_access_tokens

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15

SIGN_IN_AGAIN = (
    "Your identity provider session has expired. Sign in again to use HPC services."
)


class TokenUnavailable(Exception):
    """The user has no usable identity provider token.

    ``sign_in_provider`` names the identity provider a fresh sign-in through
    would fix it with (the stored token's), so the UI can offer that sign-in.
    """

    def __init__(self, message, sign_in_provider=None):
        super().__init__(message)
        self.sign_in_provider = sign_in_provider


class _RefreshRefused(Exception):
    """The identity provider answered a refresh with an error."""

    def __init__(self, provider, status_code, error=""):
        super().__init__(provider, status_code, error)
        self.provider = provider
        self.status_code = status_code
        self.error = error

    @property
    def token_is_dead(self) -> bool:
        # RFC 6749 5.2: invalid_grant is the refresh token itself (expired,
        # revoked, its session ended). Anything else, such as invalid_client
        # while a rotated client secret is not updated in Waldur yet, or a
        # rate limit, is not the token's fault: it may still be good.
        return self.error == "invalid_grant"


def tokens_needed() -> bool:
    """Whether any service acts as the user with a stored token.

    Only the HPC workspace does, and only towards the FirecREST endpoints staff
    have allowed.
    """
    return bool((config.FIRECREST_ALLOWED_API_URLS or "").strip())


# Storing and minting


def _session_id(token) -> str | None:
    """The identity provider session a JWT refresh token belongs to."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    return claims.get("sid") if isinstance(claims, dict) else None


def _in_another_session(previous: OAuthToken, provider, refresh_token) -> bool:
    """Whether the stored *previous* token belongs to another provider session
    than the one a sign-in just returned *refresh_token* from.

    Keycloak revokes the whole session of a refresh token, so revoking one
    from the session the user just signed in to would end the new token too.
    Without session ids to compare, it may be the same session.
    """
    if previous.provider != provider:
        return True
    old_session = _session_id(previous.refresh_token)
    new_session = _session_id(refresh_token) if refresh_token else None
    return bool(old_session and new_session and old_session != new_session)


def store_tokens(user, provider, access_token, refresh_token):
    """Keep what a successful sign-in returned, if anything needs it.

    The token it replaces is revoked when it belongs to another session.
    """
    needed = tokens_needed()
    with transaction.atomic():
        previous = OAuthToken.objects.select_for_update().filter(user=user).first()
        if needed and refresh_token:
            # Keyed on the user alone: a user signing in through another
            # provider replaces the token rather than colliding with the
            # one-per-user row.
            OAuthToken.objects.update_or_create(
                user=user,
                defaults={"provider": provider, "refresh_token": refresh_token},
            )
        elif previous is not None:
            previous.delete()
    forget_access_tokens(user.pk)
    if previous is not None and _in_another_session(previous, provider, refresh_token):
        revocation.revoke_replaced_token(
            user, previous.provider, previous.refresh_token
        )
    if needed:
        cache_access_token(user, access_token)


def get_sign_in_provider(user) -> str | None:
    """The identity provider that issued *user*'s stored token, if any."""
    return (
        OAuthToken.objects.filter(user=user).values_list("provider", flat=True).first()
    )


def get_access_token(user) -> str:
    """Return a valid access token of *user*, refreshing it if needed."""
    access_token = cached_access_token(user)
    if access_token:
        return access_token
    try:
        with transaction.atomic():
            # Lock the row: with rotating refresh tokens two concurrent
            # refreshes would have the second one fail with the used token.
            oauth_token = (
                OAuthToken.objects.select_for_update().filter(user=user).first()
            )
            if oauth_token is None:
                raise TokenUnavailable(
                    "HPC services need a sign-in through your identity provider. "
                    "Sign out and sign in again with your institutional account."
                )
            # Another worker may have refreshed while this one waited.
            access_token = cached_access_token(user)
            if access_token:
                return access_token
            access_token = _refresh(oauth_token)
            # Cached while the row is still locked: a revocation waiting for
            # the lock drops it with the user's other cached tokens.
            cache_access_token(user, access_token)
    except _RefreshRefused as e:
        # Outside the transaction, so neither the event nor the deletion is
        # rolled back with it.
        event_logger.emit(
            "Identity provider {provider} refused to refresh the token of "
            "{affected_user_username} ({status_code}).",
            event_type=EventType.OIDC_TOKEN_REFRESH_FAILED,
            event_context={
                "affected_user": user,
                "provider": e.provider,
                "status_code": e.status_code,
            },
            scopes=[user],
        )
        if not e.token_is_dead:
            raise TokenUnavailable(
                f"The identity provider did not refresh your token ({e.status_code}). "
                "Try again later; if this persists, ask support to check the "
                "identity provider setup."
            )
        revocation.forget_oauth_tokens(
            user, revocation.RevocationReason.REJECTED, notify_provider=False
        )
        raise TokenUnavailable(SIGN_IN_AGAIN, e.provider)
    return access_token


def _refresh(oauth_token: OAuthToken) -> str:
    if not oauth_token.refresh_token:
        raise TokenUnavailable(SIGN_IN_AGAIN, oauth_token.provider)
    provider = IdentityProvider.objects.filter(
        provider=oauth_token.provider, is_active=True
    ).first()
    if provider is None:
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
            timeout=REQUEST_TIMEOUT,
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
        try:
            error = response.json().get("error") or ""
        except (ValueError, AttributeError):
            error = ""
        raise _RefreshRefused(oauth_token.provider, response.status_code, error)
    data = response.json()
    # Providers that rotate refresh tokens invalidate the old one on use.
    new_refresh_token = data.get("refresh_token")
    if new_refresh_token and new_refresh_token != oauth_token.refresh_token:
        oauth_token.refresh_token = new_refresh_token
        oauth_token.save(update_fields=["refresh_token"])
    return data["access_token"]


def forget_stale_oauth_tokens():
    """Revoke tokens nothing needs: all of them while the HPC workspace is off,
    otherwise those of users who have not signed in for the configured days."""
    tokens = OAuthToken.objects.select_related("user")
    if tokens_needed():
        cutoff = timezone.now() - timedelta(days=config.OIDC_TOKEN_MAX_IDLE_DAYS)
        tokens = tokens.filter(
            Q(user__last_login__lt=cutoff) | Q(user__last_login__isnull=True)
        )
        reason = revocation.RevocationReason.IDLE
    else:
        reason = revocation.RevocationReason.DISABLED
    for oauth_token in tokens:
        revocation.forget_oauth_tokens(oauth_token.user, reason)
