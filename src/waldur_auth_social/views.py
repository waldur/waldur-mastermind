import base64
import logging
import uuid

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status, views
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from waldur_auth_social.exceptions import OAuthException
from waldur_auth_social.models import OAuthToken
from waldur_auth_social.utils import (
    create_or_update_eduteams_user,
    pull_remote_eduteams_user,
)
from waldur_core.core.views import RefreshTokenMixin, validate_authentication_method

from .log import event_logger, provider_event_type_mapping
from .serializers import AuthSerializer, RemoteEduteamsRequestSerializer

logger = logging.getLogger(__name__)

auth_social_settings = getattr(settings, 'WALDUR_AUTH_SOCIAL', {})
SMARTIDEE_SECRET = auth_social_settings.get('SMARTIDEE_SECRET')

TARA_CLIENT_ID = auth_social_settings.get('TARA_CLIENT_ID')
TARA_SECRET = auth_social_settings.get('TARA_SECRET')
TARA_SANDBOX = auth_social_settings.get('TARA_SANDBOX')

KEYCLOAK_CLIENT_ID = auth_social_settings.get('KEYCLOAK_CLIENT_ID')
KEYCLOAK_SECRET = auth_social_settings.get('KEYCLOAK_SECRET')
KEYCLOAK_TOKEN_URL = auth_social_settings.get('KEYCLOAK_TOKEN_URL')
KEYCLOAK_USERINFO_URL = auth_social_settings.get('KEYCLOAK_USERINFO_URL')
KEYCLOAK_VERIFY_SSL = auth_social_settings.get('KEYCLOAK_VERIFY_SSL')

EDUTEAMS_CLIENT_ID = auth_social_settings.get('EDUTEAMS_CLIENT_ID')
EDUTEAMS_SECRET = auth_social_settings.get('EDUTEAMS_SECRET')
EDUTEAMS_TOKEN_URL = auth_social_settings.get('EDUTEAMS_TOKEN_URL')
EDUTEAMS_USERINFO_URL = auth_social_settings.get('EDUTEAMS_USERINFO_URL')

validate_social_signup = validate_authentication_method('SOCIAL_SIGNUP')

User = get_user_model()


def generate_username():
    return uuid.uuid4().hex[:30]


class OAuthView(RefreshTokenMixin, views.APIView):
    permission_classes = []
    authentication_classes = []
    provider = None

    @validate_social_signup
    def post(self, request, format=None):
        if not self.request.user.is_anonymous:
            raise ValidationError('This view is for anonymous users only.')

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
            event_type=provider_event_type_mapping[self.provider],
            event_context={
                'provider': self.provider,
                'user': user,
                'request': request,
            },
        )
        return Response(
            {'token': token.key},
            status=created and status.HTTP_201_CREATED or status.HTTP_200_OK,
        )

    def authenticate_user(self, validated_data):
        try:
            token_response = self.get_token_response(validated_data)
        except requests.exceptions.RequestException as e:
            logger.warning('Unable to send authentication request. Error is %s', e)
            raise OAuthException(
                self.provider, 'Unable to send authentication request.'
            )

        self.check_response(token_response)

        try:
            token_data = token_response.json()
        except (ValueError, TypeError):
            raise OAuthException(
                self.provider, 'Unable to parse JSON in authentication response.'
            )

        try:
            access_token = token_data['access_token']
        except KeyError:
            raise OAuthException(
                self.provider, 'Authentication response does not contain token.'
            )

        refresh_token = token_data.get('refresh_token', '')

        try:
            user_response = self.get_user_response(access_token)
        except requests.exceptions.RequestException as e:
            logger.warning('Unable to send user info request. Error is %s', e)
            raise OAuthException(self.provider, 'Unable to send user info request.')
        self.check_response(user_response)

        try:
            user_info = user_response.json()
        except (ValueError, TypeError):
            raise OAuthException(
                self.provider, 'Unable to parse JSON in user info response.'
            )

        user, created = self.create_or_update_user(user_info)
        OAuthToken.objects.update_or_create(
            user=user,
            provider=self.provider,
            defaults={
                'access_token': access_token,
                'refresh_token': refresh_token,
            },
        )
        return user, created

    def get_token_response(self, validated_data):
        raise NotImplementedError

    def get_user_response(self, access_token):
        raise NotImplementedError

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
            raise OAuthException(self.provider, error_message, error_description)


class SmartIDeeView(OAuthView):
    provider = 'smartid.ee'

    def get_token_response(self, validated_data):
        access_token_url = 'https://id.smartid.ee/oauth/access_token'

        data = {
            'client_id': validated_data['client_id'],
            'client_secret': SMARTIDEE_SECRET,
            'redirect_uri': validated_data['redirect_uri'],
            'code': validated_data['code'],
            'grant_type': 'authorization_code',
        }
        return requests.post(access_token_url, data=data)

    def get_user_response(self, access_token):
        user_data_url = 'https://id.smartid.ee/api/v2/user_data'
        return requests.get(user_data_url, params={'access_token': access_token})

    def create_or_update_user(self, backend_user):
        """Authenticate user by civil number"""
        first_name = backend_user['firstname']
        last_name = backend_user['lastname']
        try:
            user = User.objects.get(civil_number=backend_user['idcode'])
        except User.DoesNotExist:
            created = True
            user = User.objects.create_user(
                username=generate_username(),
                # Ilja: disabling email update from smartid.ee as it comes in as a fake object for the moment.
                # email=backend_user['email'],
                first_name=first_name,
                last_name=last_name,
                civil_number=backend_user['idcode'],
                registration_method=self.provider,
            )
            user.set_unusable_password()
            user.save()
        else:
            created = False
            update_fields = set()
            if user.first_name != first_name:
                user.first_name = first_name
                update_fields.add('first_name')
            if user.last_name != last_name:
                user.last_name = last_name
                update_fields.add('last_name')
            if update_fields:
                user.save(update_fields=update_fields)
        return user, created


class TARAView(OAuthView):
    """
    See also reference documentation for TARA authentication in Estonian language:
    https://e-gov.github.io/TARA-Doku/TehnilineKirjeldus#431-identsust%C3%B5end
    """

    provider = 'tara'

    @property
    def base_url(self):
        if TARA_SANDBOX:
            return 'https://tara-test.ria.ee/oidc/'
        else:
            return 'https://tara.ria.ee/oidc/'

    def get_token_response(self, validated_data):
        user_data_url = self.base_url + 'token'

        data = {
            'grant_type': 'authorization_code',
            'redirect_uri': validated_data['redirect_uri'],
            'code': validated_data['code'],
        }

        raw_token = f'{TARA_CLIENT_ID}:{TARA_SECRET}'
        auth_token = base64.b64encode(raw_token.encode('utf-8'))

        headers = {'Authorization': b'Basic %s' % auth_token}
        return requests.post(user_data_url, data=data, headers=headers)

    def get_user_response(self, access_token):
        user_data_url = self.base_url + 'profile'
        return requests.get(user_data_url, params={'access_token': access_token})

    def create_or_update_user(self, backend_user):
        try:
            first_name = backend_user['given_name']
            last_name = backend_user['family_name']
            civil_number = backend_user['sub']
            # AMR stands for Authentication Method Reference
            details = {
                'amr': backend_user.get('amr'),
                'profile_attributes_translit': backend_user.get(
                    'profile_attributes_translit'
                ),
            }
        except KeyError as e:
            logger.warning('Unable to parse identity certificate. Error is: %s', e)
            raise OAuthException(self.provider, 'Unable to parse identity certificate.')
        try:
            user = User.objects.get(civil_number=civil_number)
        except User.DoesNotExist:
            created = True
            user = User.objects.create_user(
                username=generate_username(),
                first_name=first_name,
                last_name=last_name,
                civil_number=civil_number,
                registration_method=self.provider,
                details=details,
            )
            user.set_unusable_password()
            user.save()
        else:
            created = False
            update_fields = set()
            if user.first_name != first_name:
                user.first_name = first_name
                update_fields.add('first_name')
            if user.last_name != last_name:
                user.last_name = last_name
                update_fields.add('last_name')
            if user.details != details:
                user.details = details
                update_fields.add('details')
            if update_fields:
                user.save(update_fields=update_fields)
        return user, created


class KeycloakView(OAuthView):
    provider = 'keycloak'

    def get_token_response(self, validated_data):
        data = {
            'grant_type': 'authorization_code',
            'redirect_uri': validated_data['redirect_uri'],
            'code': validated_data['code'],
            'client_id': KEYCLOAK_CLIENT_ID,
            'client_secret': KEYCLOAK_SECRET,
        }

        return requests.post(KEYCLOAK_TOKEN_URL, data=data, verify=KEYCLOAK_VERIFY_SSL)

    def get_user_response(self, access_token):
        headers = {'Authorization': f'Bearer {access_token}'}
        return requests.get(
            KEYCLOAK_USERINFO_URL, headers=headers, verify=KEYCLOAK_VERIFY_SSL
        )

    def create_or_update_user(self, backend_user):
        # Preferred username is not unique. Sub in UUID.
        username = backend_user["sub"]
        email = backend_user.get('email')
        first_name = backend_user.get('given_name', '')
        last_name = backend_user.get('family_name', '')
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            created = True
            user = User.objects.create_user(
                username=username,
                registration_method=self.provider,
                email=email,
                first_name=first_name,
                last_name=last_name,
            )
            user.set_unusable_password()
            user.save()
        else:
            created = False
            update_fields = set()
            if user.first_name != first_name:
                user.first_name = first_name
                update_fields.add('first_name')
            if user.last_name != last_name:
                user.last_name = last_name
                update_fields.add('last_name')
            if update_fields:
                user.save(update_fields=update_fields)
        return user, created


class EduteamsView(OAuthView):
    provider = 'eduteams'

    def get_token_response(self, validated_data):
        data = {
            'grant_type': 'authorization_code',
            'redirect_uri': validated_data['redirect_uri'],
            'code': validated_data['code'],
            'client_id': EDUTEAMS_CLIENT_ID,
            'client_secret': EDUTEAMS_SECRET,
        }

        return requests.post(EDUTEAMS_TOKEN_URL, data=data)

    def get_user_response(self, access_token):
        headers = {'Authorization': f'Bearer {access_token}'}
        return requests.get(EDUTEAMS_USERINFO_URL, headers=headers)

    def create_or_update_user(self, backend_user):
        return create_or_update_eduteams_user(backend_user)


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
