import hashlib
import logging
from enum import Enum

import httpx
import jwt
from constance import config
from django.conf import settings
from django.core.cache import cache
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
MIN_TOKEN_UPDATE_INTERVAL = timezone.timedelta(seconds=60)


def refresh_token(user: models.User) -> Token:
    """
    Create new token if it does not exist yet or if it has already expired.
    Token is refreshed if it has not expired yet.
    """
    token, created = Token.objects.get_or_create(user=user)

    if user.token_lifetime:
        lifetime = timezone.timedelta(seconds=user.token_lifetime)

        if token.created < timezone.now() - lifetime:
            token.delete()
            token = Token.objects.create(user=user)
            created = True

    if not created:
        # Token is updated for each request therefore it may become bottleneck.
        # To avoid race-conditions if token is updated simulteneously,
        # and to reduce DB load, we use debouncing.
        if token.created < timezone.now() - MIN_TOKEN_UPDATE_INTERVAL:
            Token.objects.filter(key=token.key).update(created=Now())
    return token


class AuthenticationMethod(str, Enum):
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


def set_user_context(user: models.User):
    waldur_core.logging.middleware.set_current_user(user)
    waldur_core.core.middleware.set_current_user(user)
    if not Token.objects.filter(user=user).exists():
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
    def enforce_csrf(self, request):
        return  # Skip CSRF check

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            user, _ = result
            set_user_context(user)
            refresh_token(user)
        return result


class OIDCAuthentication(BaseAuthentication):
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

            cache.set(cache_key, data, timeout=cache_timeout)

        user_identifier = data.get(user_field)
        if not user_identifier:
            raise AuthenticationFailed(
                f"Token does not contain required user identifier field: '{user_field}'"
            )

        user, _ = models.User.objects.get_or_create(username=user_identifier)
        set_user_context(user)

        return (user, parsed_token)
