"""Authentication and permissions for the inbound SCIM Service Provider.

A SCIM client (Okta, Entra ID, ...) authenticates with a long-lived bearer token
tied to a staff service-account User. We accept both ``Authorization: Bearer ...``
(SCIM standard) and ``Authorization: Token ...`` (Waldur convention) so a single
``core.AuthToken`` can be reused.
"""

from constance import config
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.authtoken.models import Token
from rest_framework.permissions import BasePermission

from waldur_core.core.authentication import refresh_token, set_user_context


class ScimBearerAuthentication(BaseAuthentication):
    """Resolve ``core.AuthToken`` from Bearer or Token authorization header."""

    keyword_aliases = (b"bearer", b"token")

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if not auth or auth[0].lower() not in self.keyword_aliases:
            return None
        if len(auth) == 1:
            raise exceptions.AuthenticationFailed(
                _("Invalid token. No credentials provided.")
            )
        if len(auth) > 2:
            raise exceptions.AuthenticationFailed(
                _("Invalid token. Token string should not contain spaces.")
            )
        try:
            key = auth[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed(
                _(
                    "Invalid token header. Token string should not contain invalid characters."
                )
            )

        try:
            token = Token.objects.select_related("user").get(key=key)
        except Token.DoesNotExist:
            raise exceptions.AuthenticationFailed(_("Invalid token."))

        if not token.user.is_active:
            raise exceptions.AuthenticationFailed(_("User inactive or deleted."))

        set_user_context(token.user)
        refresh_token(token.user)
        return token.user, token

    def authenticate_header(self, request):
        return 'Bearer realm="scim"'


class IsScimStaff(BasePermission):
    """Only staff service-accounts may drive SCIM provisioning."""

    message = "SCIM provisioning requires a staff service-account token."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.is_active and user.is_staff)


class ScimFeatureEnabled(BasePermission):
    """Return 403 with a clear message when the feature flag is off.

    Returning 403 (rather than 503) keeps the response shape uniform; the message
    distinguishes the cause for operators inspecting the response body.
    """

    message = "SCIM inbound provisioning is disabled (set Constance SCIM_INBOUND_ENABLED=True)."

    def has_permission(self, request, view):
        return bool(config.SCIM_INBOUND_ENABLED)
