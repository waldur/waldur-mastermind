import hashlib
import logging
from enum import StrEnum

import httpx
import jwt
import reversion
from constance import config
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models.functions import Now
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions
from rest_framework.authentication import (
    BaseAuthentication,
    get_authorization_header,
)
from rest_framework.authentication import (
    SessionAuthentication as DRFSessionAuthentication,
)
from rest_framework.authentication import (
    TokenAuthentication as DRFTokenAuthentication,
)
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed

import waldur_core.core.middleware
import waldur_core.logging.middleware
from waldur_core.core import models
from waldur_core.core.utils import is_uuid_like

logger = logging.getLogger(__name__)

IMPERSONATED_USER_HEADER = settings.WALDUR_CORE.get(
    "REQUEST_HEADER_IMPERSONATED_USER_UUID"
)
DEFAULT_TOKEN_UPDATE_INTERVAL = timezone.timedelta(seconds=600)


def refresh_token(user: models.User) -> Token:
    """
    Create new token if it does not exist yet or if it has already expired.
    Token is refreshed if it has not expired yet.
    """
    token, created = Token.objects.get_or_create(user=user)

    if user.token_lifetime:
        lifetime = timezone.timedelta(seconds=user.token_lifetime)

        if token.created < timezone.now() - lifetime:
            # Rotate the expired token. Wrap in a savepoint and tolerate a
            # concurrent rotation: another request/Celery task may have already
            # deleted and recreated the token for this user between our read and
            # our write. Since Token.user is unique (OneToOneField), the racing
            # create() would otherwise raise IntegrityError and bubble up as a
            # 500. On collision we roll back the savepoint and reuse the token
            # the winning request created.
            try:
                with transaction.atomic():
                    token.delete()
                    token = Token.objects.create(user=user)
                created = True
            except IntegrityError:
                token = Token.objects.get(user=user)

    if not created:
        # Token is updated for each request therefore it may become bottleneck.
        # To avoid race-conditions if token is updated simultaneously,
        # and to reduce DB load, we use debouncing.
        if user.token_lifetime:
            interval = timezone.timedelta(seconds=user.token_lifetime / 2)
        else:
            interval = DEFAULT_TOKEN_UPDATE_INTERVAL
        if token.created < timezone.now() - interval:
            Token.objects.filter(key=token.key).update(created=Now())
    return token


class AuthenticationMethod(StrEnum):
    TARA = "tara"
    EDUTEAMS = "eduteams"
    KEYCLOAK = "keycloak"
    SAML2 = "saml2"
    LOCAL = "default"
    VALIMO = "valimo"


OIDC_AUTHENTICATION_METHODS = (
    AuthenticationMethod.TARA,
    AuthenticationMethod.EDUTEAMS,
    AuthenticationMethod.KEYCLOAK,
)

AUTHENTICATION_METHOD_KEY = "AUTHENTICATION_METHOD"


def set_authentication_method(
    request: HttpRequest, authentication_method: AuthenticationMethod
):
    request.session[AUTHENTICATION_METHOD_KEY] = authentication_method


def get_authentication_method(request: HttpRequest) -> AuthenticationMethod:
    return request.session.get(AUTHENTICATION_METHOD_KEY)


def can_access_admin_site(user):
    return user.is_active and (user.is_staff or user.is_support)


class AdminAuthenticationBackend:
    """
    Enables only support and staff to access admin site.
    """

    def authenticate(self, request, username, password):
        """
        Always return ``None`` to prevent authentication within this backend.
        """
        return None

    def has_perm(self, user_obj, perm, obj=None):
        return can_access_admin_site(user_obj)

    def has_module_perms(self, user_obj, app_label):
        return can_access_admin_site(user_obj)


def set_user_context(user: models.User, skip_token_check=False):
    waldur_core.logging.middleware.set_current_user(user)
    waldur_core.core.middleware.set_current_user(user)
    if not skip_token_check and not Token.objects.filter(user=user).exists():
        raise exceptions.PermissionDenied(
            "Unable to impersonate user that does not have an active session."
        )


def parse_token_from_request(request, keyword) -> str | None:
    auth = get_authorization_header(request).split()

    if not auth or auth[0].lower() != keyword:
        return None

    if len(auth) == 1:
        msg = _("Invalid token. No credentials provided.")
        raise exceptions.AuthenticationFailed(msg)
    elif len(auth) > 2:
        msg = _("Invalid token. Token string should not contain spaces.")
        raise exceptions.AuthenticationFailed(msg)

    try:
        token = auth[1].decode()
    except UnicodeError:
        msg = _(
            "Invalid token header. Token string should not contain invalid characters."
        )
        raise exceptions.AuthenticationFailed(msg)

    return token


class ImpersonationAuthentication(DRFTokenAuthentication):
    """
    Token-based authentication with impersonation.
    """

    def authenticate_credentials(self, key, impersonated_user_uuid=None):
        try:
            token = Token.objects.select_related("user").get(key=key)
        except Token.DoesNotExist:
            raise exceptions.AuthenticationFailed(_("Invalid token."))

        if not token.user.is_active:
            raise exceptions.AuthenticationFailed(_("User inactive or deleted."))

        if token.user.token_lifetime:
            lifetime = timezone.timedelta(seconds=token.user.token_lifetime)

            if token.created < timezone.now() - lifetime:
                raise exceptions.AuthenticationFailed(_("Token has expired."))

        if impersonated_user_uuid and token.user.is_staff:
            impersonated_user = models.ImpersonatedUser.all_objects.filter(
                uuid=impersonated_user_uuid
            ).first()

            if impersonated_user:
                set_user_context(impersonated_user)
                impersonated_user.impersonator = token.user
                return impersonated_user, token
            else:
                logger.warning(
                    f"Incorrect impersonated user UUID {impersonated_user_uuid}. User not found"
                )

        set_user_context(token.user)
        refresh_token(token.user)
        return token.user, token

    def authenticate(self, request):
        token = parse_token_from_request(request, b"token")
        if not token:
            return None
        impersonated_user_uuid = request.META.get(IMPERSONATED_USER_HEADER)

        if impersonated_user_uuid:
            if is_uuid_like(impersonated_user_uuid):
                return self.authenticate_credentials(
                    token, impersonated_user_uuid=impersonated_user_uuid
                )
            else:
                logger.warning(
                    f"Impersonated user UUID {impersonated_user_uuid} is not correct."
                )

        return self.authenticate_credentials(token)


class SessionAuthentication(DRFSessionAuthentication):
    # Note: the previous override silently skipped CSRF for cookie-session
    # auth, which made every state-changing API endpoint vulnerable to
    # cross-site requests from any logged-in browser. The Waldur SPA does
    # not use cookie auth (it sends Authorization: Token/Bearer), so DRF's
    # default CSRF enforcement here does not affect normal frontend usage.

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            user, _ = result
            refresh_token(user)  # Ensure token exists before setting context
            set_user_context(user)
        return result


class PATAuthentication(BaseAuthentication):
    """Authenticate requests using Personal Access Tokens.

    Accepts ``Authorization: Bearer w_...`` headers.  Only tokens
    with the ``w_`` prefix are handled; others fall through to the
    next authentication backend (e.g. OIDC).
    """

    def authenticate(self, request):
        raw_token = parse_token_from_request(request, b"bearer")
        if not raw_token:
            return None

        # Only handle tokens with the w_ prefix
        if not raw_token.startswith("w_"):
            return None

        # Quick expiry pre-check from embedded timestamp (w_<ts>_<random>)
        parts = raw_token.split("_", 2)
        if len(parts) == 3:
            try:
                if int(parts[1]) < int(timezone.now().timestamp()):
                    raise AuthenticationFailed("Invalid token.")
            except (ValueError, OverflowError):
                pass

        # Global kill-switch
        if not config.PAT_ENABLED:
            return None

        import hashlib as _hashlib

        from waldur_core.core.models import PersonalAccessToken

        token_hash = _hashlib.sha256(raw_token.encode()).hexdigest()

        try:
            pat = PersonalAccessToken.objects.select_related("user").get(
                token_hash=token_hash
            )
        except PersonalAccessToken.DoesNotExist:
            raise AuthenticationFailed("Invalid token.")

        if not pat.is_active:
            raise AuthenticationFailed("Invalid token.")

        if not pat.user.is_active:
            raise AuthenticationFailed("Invalid token.")

        if not pat.user.can_use_personal_access_tokens:
            raise AuthenticationFailed("Invalid token.")

        if pat.is_expired:
            raise AuthenticationFailed("Invalid token.")

        # Update usage stats with batched writes (debounce via cache)
        client_ip = self._get_client_ip(request)
        self._update_usage(pat, client_ip)

        set_user_context(pat.user, skip_token_check=True)
        return (pat.user, pat)

    @staticmethod
    def _get_client_ip(request):
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            return xff.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR")

    @staticmethod
    def _update_usage(pat, client_ip):
        cache_key = f"pat_usage:{pat.pk}"
        if not cache.get(cache_key):
            # Check for new IP before updating
            if pat.last_used_ip and client_ip and pat.last_used_ip != client_ip:
                from waldur_core.logging import event_logger as _event_logger
                from waldur_core.logging.enums import EventType

                _event_logger.emit(
                    f"Personal access token {pat.name} for user "
                    f"{{affected_user_username}} used from new IP {client_ip} "
                    f"(previous: {pat.last_used_ip}).",
                    event_type=EventType.PAT_USED_FROM_NEW_IP,
                    event_context={"affected_user": pat.user},
                    scopes=[pat.user],
                )

            from django.db.models import F

            PersonalAccessToken = pat.__class__
            PersonalAccessToken.objects.filter(pk=pat.pk).update(
                last_used_at=timezone.now(),
                last_used_ip=client_ip,
                use_count=F("use_count") + 1,
            )
            cache.set(cache_key, True, timeout=600)  # 10 min debounce


class OIDCAuthentication(BaseAuthentication):
    # Marker stored on `User.registration_method` for accounts governed by
    # this OIDC backend. A username collision with an account carrying a
    # different marker triggers an audited adoption (see `_adopt_local_account`)
    # rather than a silent, unrecorded takeover.
    REGISTRATION_METHOD = "oidc"

    def authenticate(self, request):
        raw_token = parse_token_from_request(request, b"bearer")
        if not raw_token:
            return None

        try:
            # We skip signature verification, since we're just reading the payload
            parsed_token = jwt.decode(
                raw_token, options={"verify_signature": False, "verify_exp": True}
            )
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed("Token has expired.")
        except jwt.DecodeError:
            raise AuthenticationFailed("Invalid JWT token.")

        introspection_url = config.OIDC_INTROSPECTION_URL
        client_id = config.OIDC_CLIENT_ID
        client_secret = config.OIDC_CLIENT_SECRET
        user_field = config.OIDC_USER_FIELD
        cache_timeout = config.OIDC_CACHE_TIMEOUT

        if not (introspection_url and client_id and client_secret):
            raise AuthenticationFailed("Introspection configuration is incomplete.")

        # Use SHA-256 to hash token to avoid very long keys
        cache_key = f"oidc_token:{hashlib.sha256(raw_token.encode()).hexdigest()}"

        data = cache.get(cache_key)
        if not data:
            try:
                response = httpx.post(
                    introspection_url,
                    data={"token": raw_token},
                    auth=(client_id, client_secret),
                    timeout=5.0,
                )

            except httpx.RequestError as e:
                raise AuthenticationFailed(
                    f"Connection to introspection endpoint failed: {str(e)}"
                )
            except Exception as e:
                raise AuthenticationFailed(f"Authentication failed: {str(e)}")

            if response.status_code != 200:
                raise AuthenticationFailed("Introspection endpoint error.")

            data = response.json()
            if not data.get("active"):
                raise AuthenticationFailed("Token is inactive or invalid.")

            # Only cache active tokens
            cache.set(cache_key, data, timeout=cache_timeout)

        user_identifier = data.get(user_field)
        if not user_identifier:
            raise AuthenticationFailed(
                f"Token does not contain required user identifier field: '{user_field}'"
            )

        user = self._get_or_provision_user(user_identifier, parsed_token)
        set_user_context(user)

        return (user, parsed_token)

    def _get_or_provision_user(
        self, user_identifier: str, parsed_token: dict
    ) -> models.User:
        """
        Look up or create the local user for an OIDC token.

        An OIDC token whose `username` claim matches a pre-existing local
        account (e.g. a `default`/SAML/social user, or an OIDC user created
        before we started tagging them) is allowed to *adopt* that account:
        the account is re-tagged with `registration_method="oidc"` so future
        logins bind cleanly. This is a deliberate design choice — it keeps
        existing users working across the OIDC rollout rather than locking
        them out.

        Because adoption of a non-OIDC account is also the shape of a
        username-takeover attempt (a trusted IdP asserting the username of a
        local staff account), every adoption is loud: it emits a WARNING log
        and records a django-reversion snapshot of the pre-adoption state, so
        operators can audit — and, if it was illegitimate, revert — the change.

        Uses `User.all_objects.get_or_create` so that (a) a disabled account is
        discovered — and rejected — rather than being hidden by the active-only
        default manager and then re-created into a unique-username collision,
        and (b) two concurrent first-time logins for the same new username race
        safely: `get_or_create` retries the lookup on `IntegrityError` instead
        of surfacing a 500.
        """
        existing, created = models.User.all_objects.get_or_create(
            username=user_identifier,
            defaults={"registration_method": self.REGISTRATION_METHOD},
        )
        if created:
            return existing

        if not existing.is_active:
            raise AuthenticationFailed("User inactive or deleted.")

        if existing.registration_method != self.REGISTRATION_METHOD:
            self._adopt_local_account(existing, parsed_token)

        return existing

    def _adopt_local_account(self, user: models.User, parsed_token: dict) -> None:
        """
        Re-tag a pre-existing non-OIDC account as OIDC-governed, leaving a
        durable audit trail (WARNING log + reversion snapshot of the pre-change
        state). Called only on a username collision; see
        `_get_or_provision_user` for the rationale.
        """
        previous_method = user.registration_method
        token_sub = parsed_token.get("sub")
        token_iss = parsed_token.get("iss")

        logger.warning(
            "OIDC is adopting pre-existing local account %r "
            "(previous registration_method=%r, is_staff=%s, is_superuser=%s) "
            "for token sub=%r iss=%r. Re-tagging as %r and recording a "
            "reversion snapshot of the pre-adoption state. If this account was "
            "not meant to be governed by OIDC, treat it as a takeover attempt "
            "and revert via the recorded revision.",
            user.username,
            previous_method,
            user.is_staff,
            user.is_superuser,
            token_sub,
            token_iss,
            self.REGISTRATION_METHOD,
        )

        # Snapshot the pre-adoption state first so an operator can revert the
        # account (e.g. after a suspected takeover) to its original
        # registration_method.
        with reversion.create_revision():
            reversion.add_to_revision(user)
            reversion.set_comment(
                f"Pre-OIDC-adoption snapshot: account was registered via "
                f"'{previous_method}' before an OIDC token (sub={token_sub!r}, "
                f"iss={token_iss!r}) adopted it."
            )

        user.registration_method = self.REGISTRATION_METHOD
        with reversion.create_revision():
            user.save(update_fields=["registration_method"])
            reversion.set_comment(
                f"OIDC adopted account previously registered via "
                f"'{previous_method}' (token sub={token_sub!r})."
            )
