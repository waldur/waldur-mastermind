import functools
import json
import math
from urllib.parse import parse_qsl, urlparse

from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils.html import format_html
from django.views import View
from drf_orjson_renderer.parsers import ORJSONParser
from drf_spectacular.utils import extend_schema
from oauth2_provider import views as oauth2_views
from oauth2_provider.exceptions import OAuthToolkitError
from oauth2_provider.http import OAuth2ResponseRedirect
from oauth2_provider.models import get_application_model, get_grant_model
from oauth2_provider.scopes import get_scopes_backend
from oauth2_provider.settings import oauth2_settings
from oauthlib.oauth2.rfc6749 import errors as oauth_errors
from rest_framework import exceptions, permissions, views
from rest_framework.response import Response

from waldur_core.core.auth_utils import is_delegated
from waldur_core.core.utils import format_homeport_link
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.server.middleware import add_public_cors_headers
from waldur_mastermind.mcp import serializers
from waldur_mastermind.mcp.oauth import (
    ALLOWED_GRANT_TYPES,
    AuthorizationRequest,
    ClientRegistrationThrottle,
    add_issuer,
    validate_authorization_request,
)
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


def error_page(error):
    return HttpResponse(
        format_html(
            "<!doctype html><title>Invalid sign-in request</title>"
            "<h1>Invalid sign-in request</h1><p>{}</p>",
            # oauthlib gives some errors, such as invalid_scope, no description.
            error.description or error.error,
        ),
        status=400,
    )


def is_registered_by_admin(client_id) -> bool:
    Application = get_application_model()
    return Application.objects.filter(
        client_id=client_id,
        registration_source=Application.RegistrationSource.MANUAL,
    ).exists()


class AuthorizationView(View):
    """Checks the client's request, then hands it to the homeport consent page."""

    def get(self, request, *args, **kwargs):
        try:
            authorization = validate_authorization_request(
                request, dict(request.GET.lists())
            )
            # Refused here, as the client never hears of a refusal at consent.
            serializer = serializers.ConsentRequestSerializer(data=request.GET)
            if not serializer.is_valid():
                raise oauth_errors.InvalidRequestError(
                    description=serializers.describe_invalid_params(serializer.errors),
                    request=authorization.oauth_request,
                )
        except oauth_errors.FatalClientError as error:
            # The client or its redirect URI can't be trusted, so stay here.
            return error_page(error)
        except oauth_errors.OAuth2Error as error:
            # Whoever registers a DCR or CIMD client picks its redirect URI, so
            # sending the browser there unasked would make this a redirector.
            if not is_registered_by_admin(error.client_id):
                return error_page(error)
            return OAuth2ResponseRedirect(
                add_issuer(request, error.in_uri(error.redirect_uri)),
                oauth2_settings.ALLOWED_REDIRECT_URI_SCHEMES,
            )
        return HttpResponseRedirect(
            format_homeport_link("oauth/consent?{query}", query=request.GET.urlencode())
        )


def get_elevated_access(user) -> str | None:
    if user.is_staff:
        return "staff"
    if user.is_support:
        return "support"
    return None


class IsOwnSignIn(permissions.BasePermission):
    """The user's own sign-in: not a PAT, an OIDC token or impersonation."""

    message = "OAuth clients can only be approved from your own sign-in."

    def has_permission(self, request, view):
        return not is_delegated(request)


class ConsentView(views.APIView):
    """What homeport's consent page reads and answers.

    Everything is checked again here, as the parameters come back through the
    browser. A rejected request gets no redirect_url, with allow or without it.
    """

    permission_classes = [permissions.IsAuthenticated, IsOwnSignIn]
    # A form without allow would read as false, so as a denial nobody made.
    parser_classes = [ORJSONParser]

    def initial(self, request, *args, **kwargs):
        if not is_mcp_enabled():
            raise Http404
        super().initial(request, *args, **kwargs)

    def parse(self, serializer_class, data) -> dict:
        serializer = serializer_class(data=data)
        if not serializer.is_valid():
            raise self.refused(
                oauth_errors.InvalidRequestError(
                    description=serializers.describe_invalid_params(serializer.errors)
                )
            )
        return dict(serializer.validated_data)

    def validate(self, request, params) -> AuthorizationRequest:
        try:
            return validate_authorization_request(request, params)
        except oauth_errors.OAuth2Error as error:
            raise self.refused(error) from error

    def issue_code(self, request, authorization: AuthorizationRequest) -> str:
        try:
            redirect_url, *_ = authorization.core.create_authorization_response(
                request, authorization.scopes, authorization.credentials, True
            )
        except OAuthToolkitError as error:
            raise self.refused(error.oauthlib_error) from error

        # oauthlib returns some errors as a redirect instead of raising them, and
        # the query alone can't tell: the client can register a code parameter.
        query = dict(parse_qsl(urlparse(redirect_url).query))
        issued = get_grant_model().objects.filter(
            application=authorization.client,
            user=request.user,
            code=query.get("code"),
        )
        if not issued.exists():
            raise exceptions.ValidationError(
                {
                    "error": query.get("error", "server_error"),
                    "error_description": query.get("error_description", ""),
                }
            )
        return redirect_url

    @staticmethod
    def refused(error):
        return exceptions.ValidationError(
            {"error": error.error, "error_description": error.description}
        )

    @extend_schema(
        parameters=[serializers.ConsentRequestSerializer],
        responses={
            200: serializers.ConsentDetailsSerializer,
            400: serializers.ConsentErrorSerializer,
        },
    )
    def get(self, request):
        """Describe the client asking for access, for the consent page."""
        params = self.parse(serializers.ConsentRequestSerializer, request.query_params)
        authorization = self.validate(request, params)

        client = authorization.client
        all_scopes = get_scopes_backend().get_all_scopes()
        is_cimd = client.registration_source == client.RegistrationSource.CIMD
        details = {
            "client_name": authorization.client_name,
            # Only a CIMD client_id is a URL. DOT doesn't keep a DCR client's URI.
            "client_host": urlparse(client.client_id).hostname if is_cimd else None,
            "redirect_host": authorization.redirect_host,
            "registration_source": client.registration_source,
            "scopes": [
                {"name": scope, "description": all_scopes[scope]}
                for scope in authorization.scopes
            ],
            "elevated_access": get_elevated_access(request.user),
        }
        return Response(serializers.ConsentDetailsSerializer(details).data)

    @extend_schema(
        request=serializers.ConsentDecisionSerializer,
        responses={
            200: serializers.ConsentResultSerializer,
            400: serializers.ConsentErrorSerializer,
        },
    )
    def post(self, request):
        """Record the user's decision and return where to send the browser."""
        # DOT rebuilds its request from this URL, so these would skip validation.
        if request.query_params:
            raise self.refused(
                oauth_errors.InvalidRequestError(
                    description="Send the parameters in the request body."
                )
            )
        params = self.parse(serializers.ConsentDecisionSerializer, request.data)
        allow = params.pop("allow")
        authorization = self.validate(request, params)
        credentials = authorization.credentials

        # So there is no grant without its audit event.
        with transaction.atomic():
            if allow:
                redirect_url = self.issue_code(request, authorization)
                event_type = EventType.OAUTH_CONSENT_GRANTED
                decision = "granted"
            else:
                # Not through DOT: its deny path adds no iss and checks nothing.
                redirect_url = add_issuer(
                    request,
                    oauth_errors.AccessDeniedError(state=credentials["state"]).in_uri(
                        credentials["redirect_uri"]
                    ),
                )
                event_type = EventType.OAUTH_CONSENT_DENIED
                decision = "denied"

            # The client's name is user-controlled and emit() formats the
            # template, so it goes through a placeholder.
            event_logger.emit(
                "User {affected_user_username} has {decision} OAuth client "
                "{client_name} access to Waldur.",
                event_type=event_type,
                event_context={
                    "affected_user": request.user,
                    "decision": decision,
                    "client_name": authorization.client_name,
                    "client_id": authorization.client.client_id,
                    # Any client can take any name, and a DCR client_id is
                    # random, so this is what tells where the browser went.
                    "redirect_host": authorization.redirect_host,
                    "scope": " ".join(authorization.scopes),
                    "resource": " ".join(authorization.resources),
                },
                scopes=[request.user],
            )

        return Response(
            serializers.ConsentResultSerializer({"redirect_url": redirect_url}).data,
            headers={"Cache-Control": "no-store"},
        )
