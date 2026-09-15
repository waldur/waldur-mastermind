"""Authentication and permissions for the inbound SCIM Service Provider.

A SCIM client (Okta, Entra ID, ...) authenticates with a long-lived bearer token
tied to a staff service-account User. We accept both ``Authorization: Bearer ...``
(SCIM standard) and ``Authorization: Token ...`` (Waldur convention) so a single
``core.AuthToken`` can be reused.

Tokens are validated exactly as for ``/api/``: an expired token is rejected and
left in place, never rotated — rotating would silently invalidate the key the IdP
is configured with.
"""

from constance import config
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import BasePermission

from waldur_core.core.authentication import (
    ImpersonationAuthentication,
    parse_token_from_request,
)


class ScimBearerAuthentication(BaseAuthentication):
    """Resolve ``core.AuthToken`` from Bearer or Token authorization header."""

    keyword_aliases = (b"bearer", b"token")

    def authenticate(self, request):
        for keyword in self.keyword_aliases:
            key = parse_token_from_request(request, keyword)
            if key:
                # Same inactive-user and token_lifetime checks as /api/, and the
                # same refresh of an active token. SCIM never impersonates.
                return ImpersonationAuthentication().authenticate_credentials(key)
        return None

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
