import base64
import logging

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status, views, viewsets
from rest_framework.exceptions import AuthenticationFailed, NotFound, ValidationError
from rest_framework.response import Response

from waldur_auth_social.exceptions import OAuthException
from waldur_auth_social.models import OAuthToken, ProviderChoices
from waldur_auth_social.utils import (
    create_or_update_oauth_user,
    pull_remote_eduteams_user,
)
from waldur_core.core import permissions as core_permissions
from waldur_core.core.views import RefreshTokenMixin

from . import models
from .log import event_logger
from .serializers import (
    AuthSerializer,
    IdentityProviderSerializer,
    RemoteEduteamsRequestSerializer,
)

logger = logging.getLogger(__name__)

User = get_user_model()


class OAuthView(RefreshTokenMixin, views.APIView):
    permission_classes = []
    authentication_classes = []
    throttle_scope = 'oauth'

    def post(self, request, provider, format=None):
        if not self.request.user.is_anonymous:
            raise ValidationError('This view is for anonymous users only.')

        if provider not in ProviderChoices.CHOICES:
            raise ValidationError(
                f'provider parameter is invalid. Valid choices are: {ProviderChoices.CHOICES}'
            )
        try:
            self.config = models.IdentityProvider.objects.get(provider=provider)
        except models.IdentityProvider.DoesNotExist:
            raise AuthenticationFailed('Identity provider is not defined.')

        if not self.config.is_active:
            raise AuthenticationFailed('Identity provider is disabled.')

        serializer = AuthSerializer(
            data={
                'client_id': request.data.get('clientId'),
                'redirect_uri': request.data.get('redirectUri'),
                'code': request.data.get('code'),
            }
        )
        serializer.is_valid(raise_exception=True)

        user, created = self.authenticate_user(serializer.validated_data)
        token = self.refresh_token(user)
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])

        event_logger.auth_social.info(
            'User {user_username} with full name {user_full_name} authenticated successfully with {provider}.',
            event_type='auth_logged_in_with_oauth',
            event_context={
                'provider': provider,
                'user': user,
                'request': request,
            },
        )
        return Response(
            {'token': token.key},
            status=created and status.HTTP_201_CREATED or status.HTTP_200_OK,
        )

    def authenticate_user(self, validated_data):
        token_data = self.get_token_data(validated_data)
        try:
            access_token = token_data['access_token']
        except KeyError:
            raise OAuthException(
                self.config.provider, 'Authentication response does not contain token.'
            )

        refresh_token = token_data.get('refresh_token', '')
        user_info = self.get_user_info(access_token)

        user, created = create_or_update_oauth_user(user_info)
        OAuthToken.objects.update_or_create(
            user=user,
            provider=self.config.provider,
            defaults={
                'access_token': access_token,
                'refresh_token': refresh_token,
            },
        )
        return user, created

    def check_response(self, response, valid_response=requests.codes.ok):
        if response.status_code != valid_response:
            try:
                data = response.json()
                error_message = data['error']
                error_description = data.get('error_description', '')
            except (TypeError, ValueError, KeyError):
                values = (response.reason, response.status_code)
                error_message = 'Message: %s, status code: %s' % values
                error_description = ''
            raise OAuthException(self.config.provider, error_message, error_description)

    def get_user_info(self, access_token):
        headers = {'Authorization': f'Bearer {access_token}'}
        try:
            user_response = requests.get(
                self.config.userinfo_url, headers=headers, verify=self.config.verify_ssl
            )
        except requests.exceptions.RequestException as e:
            logger.warning('Unable to send user info request. Error is %s', e)
            raise OAuthException(
                self.config.provider, 'Unable to send user info request.'
            )
        self.check_response(user_response)

        try:
            return user_response.json()
        except (ValueError, TypeError):
            raise OAuthException(
                self.config.provider, 'Unable to parse JSON in user info response.'
            )

    def get_token_data(self, validated_data):
        data = {
            'grant_type': 'authorization_code',
            'redirect_uri': validated_data['redirect_uri'],
            'code': validated_data['code'],
        }
        headers = None
        if self.config.provider == ProviderChoices.TARA:
            raw_token = f'{self.config.client_id}:{self.config.client_secret}'
            auth_token = base64.b64encode(raw_token.encode('utf-8'))
            headers = {'Authorization': b'Basic %s' % auth_token}
        else:
            data |= {
                'client_id': self.config.client_id,
                'client_secret': self.config.client_secret,
            }
        try:
            token_response = requests.post(
                self.config.token_url, data=data, headers=headers
            )
        except requests.exceptions.RequestException as e:
            logger.warning('Unable to send authentication request. Error is %s', e)
            raise OAuthException(
                self.config.provider, 'Unable to send authentication request.'
            )

        self.check_response(token_response)

        try:
            return token_response.json()
        except (ValueError, TypeError):
            raise OAuthException(
                self.config.provider, 'Unable to parse JSON in authentication response.'
            )


class IdentityProvidersViewSet(viewsets.ModelViewSet):
    queryset = models.IdentityProvider.objects.all()
    serializer_class = IdentityProviderSerializer
    lookup_field = 'provider'
    permission_classes = (core_permissions.IsAdminOrReadOnly,)

    def get_queryset(self):
        qs = super().get_queryset()
        if not self.request.user.is_staff:
            return qs.filter(is_active=True)
        return qs


class RemoteEduteamsView(views.APIView):
    def post(self, request, *args, **kwargs):
        if not request.user.is_staff and not request.user.is_identity_manager:
            return Response(
                'Only staff and identity manager are allowed to sync remote users.',
                status=status.HTTP_403_FORBIDDEN,
            )

        if not settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_ENABLED']:
            return Response(
                'Remote eduTEAMS user sync is disabled.',
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = RemoteEduteamsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cuid = serializer.validated_data['cuid']

        user = pull_remote_eduteams_user(cuid)
        if user is None:
            raise NotFound('User %s has not been found' % cuid)
        return Response({'uuid': user.uuid.hex})
