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
from rest_framework.exceptions import AuthenticationFailed, NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.reverse import reverse

from waldur_auth_social.const import ProviderChoices
from waldur_auth_social.exceptions import OAuthException
from waldur_auth_social.models import OAuthToken
from waldur_auth_social.utils import (
    create_or_update_oauth_user,
    pull_remote_eduteams_user,
)
from waldur_core.core import permissions as core_permissions
from waldur_core.core.authentication import refresh_token, set_authentication_method
from waldur_core.core.serializers import EmptySerializer
from waldur_core.core.utils import format_homeport_link
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType

from . import models
from .serializers import (
    AuthSerializer,
    IdentityProviderSerializer,
    RemoteEduteamsRequestSerializer,
    RemoteEduteamsUUIDSerializer,
)

logger = logging.getLogger(__name__)


OIDC_STATE_KEY = "oidc_state"

OIDC_CODE_VERIFIER_KEY = "oidc_code_verifier"


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

        oidc_state = secrets.token_urlsafe(32)
        request.session[OIDC_STATE_KEY] = oidc_state

        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": oidc_state,
        }

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

        stored_state = self.request.session.get(OIDC_STATE_KEY)
        returned_state = request.query_params.get("state")
        if not stored_state or stored_state != returned_state:
            # Potential CSRF attack - reject the request
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
                "request": request,
            },
            scopes=[user],
        )
        if config.OIDC_ACCESS_TOKEN_ENABLED:
            user_token = access_token
        else:
            user_token = token.key
        params = {"token": user_token}
        return redirect(
            format_homeport_link(
                f"oauth_login_completed/{provider}/?{urlencode(params)}"
            )
        )

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
        logger.info("Received user info: %s", user_info)

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
