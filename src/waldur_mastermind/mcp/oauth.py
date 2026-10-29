"""The OAuth authorization server for MCP clients, built on django-oauth-toolkit."""

from types import SimpleNamespace

from django.conf import settings
from oauth2_provider.oauth2_validators import (
    OAuth2Validator,
    _parse_and_validate_uri,
)
from oauthlib.oauth2.rfc6749 import errors
from rest_framework.throttling import SimpleRateThrottle

from waldur_core.configuration import config

# The only grants that act for a user who consented. client_credentials would
# give any registered client a token without anyone's consent.
ALLOWED_GRANT_TYPES = ("authorization_code", "refresh_token")


def get_resource_urls() -> list[str]:
    return config.MCP_RESOURCE_URLS.split()


def get_allowed_resources() -> set[tuple]:
    # DOT's own parser, so issuance and DOT's audience check at use time agree
    # on which spellings are the same resource.
    parsed = (_parse_and_validate_uri(url) for url in get_resource_urls())
    return {resource for resource in parsed if resource is not None}


def is_allowed_resource(uri: str, allowed: set[tuple] | None = None) -> bool:
    if allowed is None:
        allowed = get_allowed_resources()
    resource = _parse_and_validate_uri(uri)
    return resource is not None and resource in allowed


class ClientRegistrationThrottle(SimpleRateThrottle):
    """New OAuth clients per client address, registered by DCR or by CIMD alike."""

    scope = "mcp_client_registration"

    def get_ident(self, request):
        # DRF's own get_ident uses the whole X-Forwarded-For while NUM_PROXIES is
        # unset, and the client writes that header. Count from the right instead:
        # those entries were added by the deployment's proxies.
        proxies = settings.MCP_OAUTH_TRUSTED_PROXY_COUNT
        forwarded = [
            address.strip()
            for address in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
            if address.strip()
        ]
        if proxies and forwarded:
            return forwarded[-min(proxies, len(forwarded))]
        return request.META.get("REMOTE_ADDR")

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class ThrottledCIMDPermission:
    """Limits how often unknown CIMD URLs are fetched and saved as clients.

    DOT asks this before every fetch, so clients it already knows don't count.
    """

    def has_permission(self, request, client_id) -> bool:
        # DOT passes the oauthlib request, whose headers are a copy of Django's META.
        client = SimpleNamespace(META=dict(request.headers))
        return ClientRegistrationThrottle().allow_request(client, None)


class MCPOAuth2Validator(OAuth2Validator):
    def validate_grant_type(
        self, client_id, grant_type, client, request, *args, **kwargs
    ):
        if grant_type not in ALLOWED_GRANT_TYPES:
            return False
        return super().validate_grant_type(
            client_id, grant_type, client, request, *args, **kwargs
        )

    def validate_code(self, client_id, code, client, request, *args, **kwargs):
        # DOT doesn't check is_active, so a deactivated user's pending code or
        # refresh token would keep minting access tokens.
        valid = super().validate_code(client_id, code, client, request, *args, **kwargs)
        return valid and request.user.is_active

    def validate_refresh_token(self, refresh_token, client, request, *args, **kwargs):
        valid = super().validate_refresh_token(
            refresh_token, client, request, *args, **kwargs
        )
        return valid and request.user is not None and request.user.is_active

    def _validate_resource_uris(self, request, resources):
        # Also runs on resources inherited from the grant or refresh token, so a
        # resource dropped from MCP_RESOURCE_URLS stops being refreshable.
        super()._validate_resource_uris(request, resources)
        allowed = get_allowed_resources()
        for resource in resources:
            if not is_allowed_resource(resource, allowed):
                raise errors.CustomOAuth2Error(
                    error="invalid_target",
                    description=f"The resource '{resource}' is not served here.",
                    request=request,
                )

    def _check_and_set_request_resource(self, request):
        super()._check_and_set_request_resource(request)
        if request.resource:
            return
        # A client that names no resource still gets a token bound to the MCP
        # endpoint, never one that is valid everywhere. Only the first URL, so
        # dropping another one later doesn't break these sessions' refresh.
        request.resource = get_resource_urls()[:1]
        if not request.resource:
            raise errors.CustomOAuth2Error(
                error="invalid_target",
                description="No MCP resource is configured.",
                request=request,
            )
        # A malformed setting fails here rather than yielding a token that can
        # never be used or refreshed.
        self._validate_resource_uris(request, request.resource)
