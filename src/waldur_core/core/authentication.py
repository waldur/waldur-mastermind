import logging
from enum import Enum

import rest_framework.authentication
from django.conf import settings
from django.db.models.functions import Now
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions
from rest_framework.authtoken.models import Token

import waldur_core.core.middleware
import waldur_core.logging.middleware
from waldur_core.core import models
from waldur_core.core.utils import is_uuid_like

logger = logging.getLogger(__name__)

TOKEN_KEY = settings.WALDUR_CORE.get("TOKEN_KEY", "x-auth-token")
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


class AuthenticationBackend:
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


class TokenAuthentication(rest_framework.authentication.TokenAuthentication):
    """
    Custom token-based authentication.

    Use TOKEN_KEY from request query parameters if authentication token was not found in header.
    """

    def get_authorization_value(self, request):
        auth = rest_framework.authentication.get_authorization_header(request)
        if not auth:
            auth = request.query_params.get(TOKEN_KEY, "")
        return auth

    def authenticate_credentials(self, key, impersonated_user_uuid=None):
        model = self.get_model()
        try:
            token = model.objects.select_related("user").get(key=key)
        except model.DoesNotExist:
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
        auth = self.get_authorization_value(request).split()

        if not auth or auth[0].lower() != b"token":
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


class SessionAuthentication(rest_framework.authentication.SessionAuthentication):
    def enforce_csrf(self, request):
        return  # Skip CSRF check

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            user, _ = result
            set_user_context(user)
            refresh_token(user)
        return result
