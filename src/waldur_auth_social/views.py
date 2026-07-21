import base64
import hashlib
import logging
import secrets
from urllib.parse import urlencode

import requests
from constance import config
from django.conf import settings
from django.shortcuts import redirect
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import AuthenticationFailed, NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.reverse import reverse

from waldur_auth_social.claim_mapping import (
    generate_default_mapping,
    get_suggested_scopes,
    get_waldur_field_suggestions,
)
from waldur_auth_social.const import ProviderChoices
from waldur_auth_social.exceptions import OAuthException
from waldur_auth_social.models import OAuthToken
from waldur_auth_social.utils import (
    create_or_update_bridge_user,
    create_or_update_oauth_user,
    get_identity_bridge_stats,
    pull_remote_eduteams_user,
    remove_user_from_isd,
    validate_and_get_redirect_url,
)
from waldur_core.core import permissions as core_permissions
from waldur_core.core.authentication import refresh_token, set_authentication_method
from waldur_core.core.models import TokenExchangeCode, User
from waldur_core.core.permissions import PATScopeAwareIsAdminUser
from waldur_core.core.serializers import EmptySerializer
from waldur_core.core.user_attributes import get_federated_identity_sync_allowed_fields
from waldur_core.core.views import login_failed
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.serializers import IdentityBridgeStatsSerializer

from . import models
from .serializers import (
    AuthSerializer,
    DiscoverMetadataRequestSerializer,
    DiscoverMetadataResponseSerializer,
    IdentityBridgeAllowedFieldsSerializer,
    IdentityBridgeRemoveResultSerializer,
    IdentityBridgeRemoveSerializer,
    IdentityBridgeRequestSerializer,
    IdentityBridgeResultSerializer,
    IdentityProviderSerializer,
    RemoteEduteamsRequestSerializer,
    RemoteEduteamsUUIDSerializer,
)

logger = logging.getLogger(__name__)


OIDC_STATE_KEY = "oidc_state"

OIDC_CODE_VERIFIER_KEY = "oidc_code_verifier"

OIDC_REFERRER_KEY = "oidc_referrer"

OIDC_RETURN_URL_KEY = "oidc_return_url"


def generate_code_challenge(code_verifier):
    """
    Generate a code challenge from the code verifier using S256 method.
    """
    code_challenge = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(code_challenge).decode("utf-8").replace("=", "")


class BaseOAuthView(generics.GenericAPIView):
    permission_classes = []
    authentication_classes = []
    throttle_scope = "oauth"
    serializer_class = EmptySerializer

    def validate_config(self, provider):
        if not self.request.user.is_anonymous:
            raise ValidationError("This view is for anonymous users only.")

        if provider not in ProviderChoices.CHOICES:
            raise ValidationError(
                f"provider parameter is invalid. Valid choices are: {ProviderChoices.CHOICES}"
            )
        try:
            self.config = models.IdentityProvider.objects.get(provider=provider)
        except models.IdentityProvider.DoesNotExist:
            raise AuthenticationFailed("Identity provider is not defined.")

        if not self.config.is_active:
            raise AuthenticationFailed("Identity provider is disabled.")


class OAuthViewInit(BaseOAuthView):
    def get(self, request, provider, format=None):
        """
        Redirect user to OIDC authorization endpoint
        """
        self.validate_config(provider)
        redirect_uri = reverse(f"auth_{provider}_complete", request=request)
        scope = f"openid {self.config.extra_scope or ''}".strip()

        # Flush the old session before writing the new OIDC state.
        # This creates a fresh session key so concurrent requests that already cannot overwrite the state.
        request.session.flush()

        oidc_state = secrets.token_urlsafe(32)
        request.session[OIDC_STATE_KEY] = oidc_state

        # Store return URL from query parameter (higher priority) or referrer header
        return_url = request.query_params.get("return_url")
        if return_url:
            request.session[OIDC_RETURN_URL_KEY] = return_url
        else:
            # Fall back to HTTP Referer header
            referrer = request.META.get("HTTP_REFERER")
            if referrer:
                request.session[OIDC_REFERRER_KEY] = referrer

        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": oidc_state,
        }

        # Pass ui_locales for Keycloak language hint (OIDC standard parameter)
        ui_locales = request.query_params.get("ui_locales")
        if ui_locales:
            params["ui_locales"] = ui_locales

        if self.config.enable_pkce:
            code_verifier = secrets.token_urlsafe(32)
            code_challenge = generate_code_challenge(code_verifier)

            request.session[OIDC_CODE_VERIFIER_KEY] = code_verifier

            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

        authorization_url = f"{self.config.auth_url}?{urlencode(params)}"
        return redirect(authorization_url)


class OAuthViewComplete(BaseOAuthView):
    @extend_schema(
        parameters=[
            OpenApiParameter(name="state", type=str, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="code", type=str, location=OpenApiParameter.QUERY),
        ]
    )
    def get(self, request, provider, format=None):
        self.validate_config(provider)
        try:
            return self._complete_login(request, provider)
        except OAuthException as e:
            # The complete endpoint is reached via a top-level browser navigation
            # (the IdP redirects here directly), so raising a DRF exception would
            # render raw JSON in the browser. For user-facing messages (e.g. the
            # configurable uninvited-user block message) redirect to the Homeport
            # login-failed page so the message is shown consistently with the rest
            # of the UI. Other errors keep their default rendering.
            if getattr(e, "user_facing", False):
                return login_failed(e.user_message)
            raise

    def _complete_login(self, request, provider):
        stored_state = self.request.session.get(OIDC_STATE_KEY)
        returned_state = request.query_params.get("state")
        if not stored_state or stored_state != returned_state:
            # Potential CSRF attack - reject the request
            logger.warning(
                "Invalid auth state for provider %s: "
                "stored_state=%r, returned_state=%r, session_key=%r",
                provider,
                stored_state,
                returned_state,
                request.session.session_key,
            )
            raise OAuthException(self.config.provider, "Invalid auth state.")
        redirect_uri = reverse(f"auth_{provider}_complete", request=request)
        serializer = AuthSerializer(
            data={
                "client_id": self.config.client_id,
                "redirect_uri": redirect_uri,
                "code": request.query_params.get("code"),
            }
        )
        serializer.is_valid(raise_exception=True)

        user, created, access_token = self.authenticate_user(serializer.validated_data)
        token = refresh_token(user)
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        set_authentication_method(request, provider)

        event_logger.emit(
            "User {user_username} with full name {user_full_name} authenticated successfully with {provider}.",
            event_type=EventType.AUTH_LOGGED_IN_WITH_OAUTH,
            event_context={
                "provider": provider,
                "user": user,
            },
            scopes=[user],
        )
        if config.OIDC_ACCESS_TOKEN_ENABLED:
            exchange_code = TokenExchangeCode.generate_code(
                user=user, external_token=access_token
            )
        else:
            exchange_code = TokenExchangeCode.generate_code(user=user, token=token)
        params = {"code": exchange_code.uuid.hex}

        # Get the stored return_url or referrer from session
        stored_return_url = request.session.get(OIDC_RETURN_URL_KEY)
        stored_referrer = request.session.get(OIDC_REFERRER_KEY)

        # Validate and get the appropriate redirect URL (return_url takes priority)
        redirect_base_url = validate_and_get_redirect_url(
            self.config, stored_referrer, stored_return_url
        )

        # Build the full redirect URL
        redirect_path = f"oauth_login_completed/{provider}/?{urlencode(params)}"
        full_redirect_url = f"{redirect_base_url.rstrip('/')}/{redirect_path}"

        return redirect(full_redirect_url)

    def authenticate_user(self, validated_data):
        token_data = self.get_token_data(validated_data)
        try:
            access_token = token_data["access_token"]
        except KeyError:
            raise OAuthException(
                self.config.provider, "Authentication response does not contain token."
            )

        refresh_token = token_data.get("refresh_token", "")
        user_info = self.get_user_info(access_token)
        logger.debug("Received user info: %s", user_info)

        user, created = create_or_update_oauth_user(self.config, user_info)

        if config.AUTO_APPROVE_USER_TOS and user.agreement_date is None:
            user.agreement_date = timezone.now()
            user.save(update_fields=["agreement_date"])

        OAuthToken.objects.update_or_create(
            user=user,
            provider=self.config.provider,
            defaults={
                "access_token": access_token,
                "refresh_token": refresh_token,
            },
        )
        return user, created, access_token

    def check_response(self, response, valid_response=requests.codes.ok):
        if response.status_code != valid_response:
            try:
                data = response.json()
                error_message = data["error"]
                error_description = data.get("error_description", "")
            except (requests.JSONDecodeError, TypeError, KeyError):
                values = (response.reason, response.status_code)
                error_message = "Message: {}, status code: {}".format(*values)
                error_description = ""
            raise OAuthException(self.config.provider, error_message, error_description)

    def get_user_info(self, access_token):
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            user_response = requests.get(
                self.config.userinfo_url, headers=headers, verify=self.config.verify_ssl
            )
        except requests.exceptions.RequestException as e:
            logger.warning("Unable to send user info request. Error is %s", e)
            raise OAuthException(
                self.config.provider, "Unable to send user info request."
            )
        self.check_response(user_response)

        try:
            return user_response.json()
        except requests.JSONDecodeError:
            raise OAuthException(
                self.config.provider, "Unable to parse JSON in user info response."
            )

    def get_token_data(self, validated_data):
        data = {
            "grant_type": "authorization_code",
            "redirect_uri": validated_data["redirect_uri"],
            "code": validated_data["code"],
        }
        if self.config.enable_pkce:
            code_verifier = self.request.session.get(OIDC_CODE_VERIFIER_KEY)
            if not code_verifier:
                raise OAuthException(self.config.provider, "PKCE verification failed.")
            data["code_verifier"] = code_verifier
        headers = None
        if self.config.provider == ProviderChoices.TARA:
            raw_token = f"{self.config.client_id}:{self.config.client_secret}"
            auth_token = base64.b64encode(raw_token.encode("utf-8"))
            headers = {"Authorization": b"Basic %s" % auth_token}
        else:
            data |= {
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
            }
        try:
            token_response = requests.post(
                self.config.token_url,
                data=data,
                headers=headers,
                verify=self.config.verify_ssl,
            )
        except requests.exceptions.RequestException as e:
            logger.warning("Unable to send authentication request. Error is %s", e)
            raise OAuthException(
                self.config.provider, "Unable to send authentication request."
            )

        self.check_response(token_response)

        try:
            return token_response.json()
        except requests.JSONDecodeError:
            raise OAuthException(
                self.config.provider, "Unable to parse JSON in authentication response."
            )


class IdentityProvidersViewSet(viewsets.ModelViewSet):
    queryset = models.IdentityProvider.objects.all()
    serializer_class = IdentityProviderSerializer
    lookup_field = "provider"
    permission_classes = (core_permissions.IsAdminOrReadOnly,)

    def get_queryset(self):
        qs = super().get_queryset()
        if not self.request.user.is_staff:
            return qs.filter(is_active=True)
        return qs

    @extend_schema(
        summary="Discover OIDC provider metadata",
        description="Fetches OIDC discovery metadata from the provider and returns "
        "supported claims, scopes, and suggested mappings to Waldur User fields. "
        "Use this to configure attribute_mapping when setting up a new identity provider.",
        request=DiscoverMetadataRequestSerializer,
        responses={200: DiscoverMetadataResponseSerializer},
    )
    @action(
        detail=False,
        methods=["post"],
        permission_classes=[PATScopeAwareIsAdminUser],
    )
    def discover_metadata(self, request):
        serializer = DiscoverMetadataRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        discovery_url = serializer.validated_data["discovery_url"]
        verify_ssl = serializer.validated_data["verify_ssl"]

        # Fetch OIDC discovery document
        try:
            response = requests.get(discovery_url, verify=verify_ssl, timeout=10)
            response.raise_for_status()
        except requests.exceptions.SSLError:
            raise ValidationError(
                {
                    "discovery_url": "SSL certificate verification failed. "
                    "Set verify_ssl to false if using a self-signed certificate."
                }
            )
        except requests.exceptions.ConnectionError:
            raise ValidationError(
                {"discovery_url": "Unable to connect to the discovery URL."}
            )
        except requests.exceptions.Timeout:
            raise ValidationError(
                {
                    "discovery_url": "Request timed out while fetching discovery document."
                }
            )
        except requests.exceptions.RequestException as e:
            raise ValidationError(
                {"discovery_url": f"Unable to fetch discovery document: {e}"}
            )

        try:
            discovery_doc = response.json()
        except requests.JSONDecodeError:
            raise ValidationError(
                {"discovery_url": "Invalid JSON in discovery response."}
            )

        # Extract claims and scopes (these are optional in OIDC spec)
        claims_supported = discovery_doc.get("claims_supported", [])
        scopes_supported = discovery_doc.get("scopes_supported", [])

        # Extract endpoints
        try:
            endpoints = {
                "authorization_endpoint": discovery_doc["authorization_endpoint"],
                "token_endpoint": discovery_doc["token_endpoint"],
                "userinfo_endpoint": discovery_doc["userinfo_endpoint"],
            }
            # Optional endpoints
            if "end_session_endpoint" in discovery_doc:
                endpoints["end_session_endpoint"] = discovery_doc[
                    "end_session_endpoint"
                ]
            if "jwks_uri" in discovery_doc:
                endpoints["jwks_uri"] = discovery_doc["jwks_uri"]
        except KeyError as e:
            raise ValidationError(
                {
                    "discovery_url": f"Missing required endpoint in discovery document: {e}"
                }
            )

        # Generate suggestions
        waldur_fields = get_waldur_field_suggestions(claims_supported)
        suggested_scopes = get_suggested_scopes(claims_supported, scopes_supported)

        response_data = {
            "claims_supported": claims_supported,
            "scopes_supported": scopes_supported,
            "endpoints": endpoints,
            "waldur_fields": waldur_fields,
            "suggested_scopes": suggested_scopes,
        }

        response_serializer = DiscoverMetadataResponseSerializer(response_data)
        return Response(response_serializer.data)

    discover_metadata_serializer_class = DiscoverMetadataRequestSerializer

    @extend_schema(
        summary="Generate default attribute mapping",
        description="Generates a suggested attribute_mapping configuration based on "
        "the claims supported by an OIDC provider. This can be used as a starting "
        "point when creating a new identity provider.",
        request=DiscoverMetadataRequestSerializer,
        responses={
            200: {
                "type": "object",
                "properties": {
                    "attribute_mapping": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Suggested mapping of Waldur fields to OIDC claims",
                    },
                    "extra_scope": {
                        "type": "string",
                        "description": "Suggested scopes to request (space-separated)",
                    },
                },
            }
        },
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="generate-mapping",
        permission_classes=[PATScopeAwareIsAdminUser],
    )
    def generate_mapping(self, request):
        serializer = DiscoverMetadataRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        discovery_url = serializer.validated_data["discovery_url"]
        verify_ssl = serializer.validated_data["verify_ssl"]

        # Fetch OIDC discovery document
        try:
            response = requests.get(discovery_url, verify=verify_ssl, timeout=10)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise ValidationError(
                {"discovery_url": f"Unable to fetch discovery document: {e}"}
            )

        try:
            discovery_doc = response.json()
        except requests.JSONDecodeError:
            raise ValidationError(
                {"discovery_url": "Invalid JSON in discovery response."}
            )

        claims_supported = discovery_doc.get("claims_supported", [])
        scopes_supported = discovery_doc.get("scopes_supported", [])

        # Generate mapping and scopes
        attribute_mapping = generate_default_mapping(claims_supported)
        suggested_scopes = get_suggested_scopes(claims_supported, scopes_supported)

        # Format extra_scope as space-separated string (excluding 'openid' which is implicit)
        extra_scope = " ".join(s for s in suggested_scopes if s != "openid")

        return Response(
            {
                "attribute_mapping": attribute_mapping,
                "extra_scope": extra_scope,
            }
        )

    generate_mapping_serializer_class = DiscoverMetadataRequestSerializer


class RemoteEduteamsView(generics.GenericAPIView):
    filter_backends = []
    pagination_class = None

    @extend_schema(
        description="Allows to pull user details from remote eduTEAMS instance.",
        request=RemoteEduteamsRequestSerializer,
        responses={200: RemoteEduteamsUUIDSerializer},
    )
    def post(self, request, *args, **kwargs):
        if not request.user.is_staff and not request.user.is_identity_manager:
            return Response(
                "Only staff and identity manager are allowed to sync remote users.",
                status=status.HTTP_403_FORBIDDEN,
            )

        if not settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_ENABLED"]:
            return Response(
                "Remote eduTEAMS user sync is disabled.",
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = RemoteEduteamsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cuid = serializer.validated_data["cuid"]

        user, created = pull_remote_eduteams_user(cuid)
        if user is None:
            raise NotFound("User %s has not been found" % cuid)

        # Disable notifications for newly created users
        if created and user.notifications_enabled:
            user.notifications_enabled = False
            user.save(update_fields=["notifications_enabled"])

        return Response({"uuid": user.uuid.hex})


class IdentityBridgeView(generics.GenericAPIView):
    """Push-based Identity Bridge API for ISD user attribute synchronization."""

    filter_backends = []
    pagination_class = None
    serializer_class = IdentityBridgeRequestSerializer

    @extend_schema(
        summary="Push user attributes from an ISD",
        description=(
            "Allows Identity Service Domains (ISDs) to push user attributes to Waldur. "
            "Creates or updates a user based on username (CUID). "
            "Requires FEDERATED_IDENTITY_SYNC_ENABLED to be True. "
            "Caller must be staff or an identity manager with the declared source in managed_isds."
        ),
        request=IdentityBridgeRequestSerializer,
        responses={200: IdentityBridgeResultSerializer},
    )
    def post(self, request, *args, **kwargs):
        # 1. Check feature flag
        if not config.FEDERATED_IDENTITY_SYNC_ENABLED:
            return Response(
                "Identity Bridge is disabled.",
                status=status.HTTP_403_FORBIDDEN,
            )

        # 2. Check caller permissions
        caller = request.user
        if not caller.is_staff and not caller.is_identity_manager:
            return Response(
                "Only staff and identity managers are allowed to use the Identity Bridge.",
                status=status.HTTP_403_FORBIDDEN,
            )

        # 3. Validate request
        serializer = IdentityBridgeRequestSerializer(
            data=request.data, context={"request": request, "view": self}
        )
        serializer.is_valid(raise_exception=True)

        username = serializer.validated_data["username"]
        source = serializer.validated_data["source"]

        # 4. Check ISD scope: non-staff must have source in managed_isds
        if not caller.is_staff:
            managed_isds = getattr(caller, "managed_isds", []) or []
            if source not in managed_isds:
                return Response(
                    f"Source '{source}' is not in your managed ISDs.",
                    status=status.HTTP_403_FORBIDDEN,
                )

        # 5. Extract attribute payload (exclude meta fields)
        attributes = {
            k: v
            for k, v in serializer.validated_data.items()
            if k not in ("username", "source")
        }

        # 6. Create or update user
        user, created, updated_fields = create_or_update_bridge_user(
            username, attributes, source
        )

        # 7. Return response
        response_data = {
            "uuid": user.uuid.hex,
            "created": created,
            "updated_fields": sorted(updated_fields),
        }
        return Response(response_data, status=status.HTTP_200_OK)


class IdentityBridgeRemoveView(generics.GenericAPIView):
    """Remove a user from an ISD via the Identity Bridge."""

    filter_backends = []
    pagination_class = None
    serializer_class = IdentityBridgeRemoveSerializer

    @extend_schema(
        summary="Remove a user from an ISD",
        description=(
            "Signals that a user has been removed from an ISD. "
            "Removes the source from active_isds, clears attributes owned by that source, "
            "and deactivates the user if no ISDs remain (configurable via FEDERATED_IDENTITY_DEACTIVATION_POLICY). "
            "Requires FEDERATED_IDENTITY_SYNC_ENABLED to be True. "
            "Caller must be staff or an identity manager with the declared source in managed_isds."
        ),
        request=IdentityBridgeRemoveSerializer,
        responses={200: IdentityBridgeRemoveResultSerializer},
    )
    def post(self, request, *args, **kwargs):
        # 1. Check feature flag
        if not config.FEDERATED_IDENTITY_SYNC_ENABLED:
            return Response(
                "Identity Bridge is disabled.",
                status=status.HTTP_403_FORBIDDEN,
            )

        # 2. Check caller permissions
        caller = request.user
        if not caller.is_staff and not caller.is_identity_manager:
            return Response(
                "Only staff and identity managers are allowed to use the Identity Bridge.",
                status=status.HTTP_403_FORBIDDEN,
            )

        # 3. Validate request
        serializer = IdentityBridgeRemoveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        username = serializer.validated_data["username"]
        source = serializer.validated_data["source"]

        # 4. Check ISD scope: non-staff must have source in managed_isds
        if not caller.is_staff:
            managed_isds = getattr(caller, "managed_isds", []) or []
            if source not in managed_isds:
                return Response(
                    f"Source '{source}' is not in your managed ISDs.",
                    status=status.HTTP_403_FORBIDDEN,
                )

        # 5. Look up the user
        try:
            user = User.all_objects.get(username=username)
        except User.DoesNotExist:
            raise NotFound(f"User {username} not found.")

        # 6. Remove from ISD
        deactivated = remove_user_from_isd(user, source)

        # 7. Return response
        response_data = {
            "uuid": user.uuid.hex,
            "deactivated": deactivated,
        }
        return Response(response_data, status=status.HTTP_200_OK)


class IdentityBridgeStatsView(generics.GenericAPIView):
    """System-wide Identity Bridge statistics for staff users."""

    filter_backends = []
    pagination_class = None
    serializer_class = IdentityBridgeStatsSerializer

    @extend_schema(
        summary="Get Identity Bridge statistics",
        description=(
            "Returns system-wide statistics about the Identity Bridge: "
            "feature configuration, per-ISD user counts, stale attribute detection, "
            "and total federated user counts. Staff only."
        ),
        responses={200: IdentityBridgeStatsSerializer},
    )
    def get(self, request, *args, **kwargs):
        if not request.user.is_staff:
            return Response(
                "Only staff users can view Identity Bridge statistics.",
                status=status.HTTP_403_FORBIDDEN,
            )

        data = get_identity_bridge_stats()
        return Response(data, status=status.HTTP_200_OK)


class IdentityBridgeAllowedFieldsView(generics.GenericAPIView):
    """Returns the list of attribute fields accepted by the Identity Bridge."""

    filter_backends = []
    pagination_class = None
    serializer_class = IdentityBridgeAllowedFieldsSerializer

    @extend_schema(
        summary="Get allowed Identity Bridge fields",
        description=(
            "Returns the list of user attribute fields that the Identity Bridge "
            "currently accepts. Useful for clients to pre-filter payloads. "
            "Requires staff or identity manager permissions."
        ),
        responses={200: IdentityBridgeAllowedFieldsSerializer},
    )
    def get(self, request, *args, **kwargs):
        if not config.FEDERATED_IDENTITY_SYNC_ENABLED:
            return Response(
                "Identity Bridge is disabled.",
                status=status.HTTP_403_FORBIDDEN,
            )
        caller = request.user
        if not caller.is_staff and not caller.is_identity_manager:
            return Response(
                "Only staff and identity managers can access this.",
                status=status.HTTP_403_FORBIDDEN,
            )
        allowed = sorted(get_federated_identity_sync_allowed_fields())
        serializer = IdentityBridgeAllowedFieldsSerializer({"allowed_fields": allowed})
        return Response(serializer.data)
