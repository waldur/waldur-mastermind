import functools
import json
import math

from django.http import HttpResponse, JsonResponse
from django.views import View
from oauth2_provider import views as oauth2_views

from waldur_core.server.middleware import add_public_cors_headers
from waldur_mastermind.mcp.oauth import ALLOWED_GRANT_TYPES, ClientRegistrationThrottle
from waldur_mastermind.mcp.utils import is_mcp_enabled


def public_oauth_endpoint(view):
    """Answers 404 while MCP_ENABLED is off, and lets any origin read the answer.

    Browser-based MCP clients call these endpoints from their own origin. They
    send no cookies, so allowing every origin exposes nothing.
    """

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_mcp_enabled():
            response = JsonResponse({"detail": "Not found."}, status=404)
        else:
            response = view(request, *args, **kwargs)
        return add_public_cors_headers(response)

    return wrapper


DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def disallowed_grant_types_response(request):
    """Refuses client metadata asking for grants that need no user's consent."""
    try:
        grant_types = json.loads(request.body).get("grant_types")
    except (ValueError, AttributeError):
        grant_types = None
    # Malformed lists fall through to DOT, which answers invalid_client_metadata.
    if not isinstance(grant_types, list) or not all(
        isinstance(grant_type, str) for grant_type in grant_types
    ):
        return None
    if not set(grant_types) <= set(ALLOWED_GRANT_TYPES):
        return JsonResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "Only the authorization_code and "
                "refresh_token grant types are supported.",
            },
            status=400,
        )
    return None


class TokenView(oauth2_views.TokenView):
    def post(self, request, *args, **kwargs):
        # DOT reads device_code unguarded for this grant, so a request without
        # it would be a 500. The device flow isn't offered here anyway.
        if request.POST.get("grant_type") == DEVICE_CODE_GRANT:
            return JsonResponse(
                {"error": "unsupported_grant_type"},
                status=400,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        return super().post(request, *args, **kwargs)

    def create_token_response(self, request):
        url, headers, body, status = super().create_token_response(request)
        # DOT drops oauthlib's headers for errors raised while saving the token,
        # such as invalid_target, so they would go out as text/html.
        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            **headers,
        }
        return url, headers, body, status


class DynamicClientRegistrationView(oauth2_views.DynamicClientRegistrationView):
    """Open RFC 7591 registration, limited to grants that need a user's consent."""

    def post(self, request, *args, **kwargs):
        throttle = ClientRegistrationThrottle()
        if not throttle.allow_request(request, self):
            return JsonResponse(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "Too many registrations from this "
                    "address. Try again later.",
                },
                status=429,
                headers={"Retry-After": str(math.ceil(throttle.wait()))},
            )
        return disallowed_grant_types_response(request) or super().post(
            request, *args, **kwargs
        )


class DynamicClientRegistrationManagementView(
    oauth2_views.DynamicClientRegistrationManagementView
):
    """RFC 7592 updates, held to the same grant types as registration."""

    def put(self, request, client_id, *args, **kwargs):
        # Authenticate first, so a caller without the registration token still
        # gets 401 rather than learning how the metadata is validated.
        auth = self._authenticate_registration_request(request, client_id)
        if isinstance(auth, HttpResponse):
            return auth
        return disallowed_grant_types_response(request) or super().put(
            request, client_id, *args, **kwargs
        )


class AuthorizationView(View):
    """Reserves the advertised authorize URL until the consent bridge (#806) serves it."""

    def get(self, request, *args, **kwargs):
        return JsonResponse(
            {
                "error": "temporarily_unavailable",
                "error_description": "Authorization is not available yet.",
            },
            status=503,
        )
