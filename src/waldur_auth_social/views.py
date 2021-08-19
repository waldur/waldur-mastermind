import base64
import logging
import uuid

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import generics, status, views
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

from waldur_auth_social.models import OAuthToken
from waldur_core.core.models import SshPublicKey
from waldur_core.core.views import RefreshTokenMixin, validate_authentication_method

from . import tasks
from .log import event_logger, provider_event_type_mapping
from .serializers import (
    ActivationSerializer,
    AuthSerializer,
    RegistrationSerializer,
    RemoteEduteamsRequestSerializer,
)

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

EDUTEAMS_CLIENT_ID = auth_social_settings.get('EDUTEAMS_CLIENT_ID')
EDUTEAMS_SECRET = auth_social_settings.get('EDUTEAMS_SECRET')
EDUTEAMS_TOKEN_URL = auth_social_settings.get('EDUTEAMS_TOKEN_URL')
EDUTEAMS_USERINFO_URL = auth_social_settings.get('EDUTEAMS_USERINFO_URL')

validate_social_signup = validate_authentication_method('SOCIAL_SIGNUP')
validate_local_signup = validate_authentication_method('LOCAL_SIGNUP')

User = get_user_model()


class OAuthException(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED

    def __init__(self, provider, error_message, error_description=None):
        self.message = '%s error: %s' % (provider, error_message)
        if error_description:
            self.message = '%s (%s)' % (self.message, error_description)
        super(OAuthException, self).__init__(detail=self.message)

    def __str__(self):
        return self.message


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

        event_logger.auth_social.info(
            'User {user_username} with full name {user_full_name} authenticated successfully with {provider}.',
            event_type=provider_event_type_mapping[self.provider],
            event_context={'provider': self.provider, 'user': user,},
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
            defaults={'access_token': access_token, 'refresh_token': refresh_token,},
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
        """ Authenticate user by civil number """
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

        raw_token = '%s:%s' % (TARA_CLIENT_ID, TARA_SECRET)
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

        return requests.post(KEYCLOAK_TOKEN_URL, data=data)

    def get_user_response(self, access_token):
        headers = {'Authorization': f'Bearer {access_token}'}
        return requests.get(KEYCLOAK_USERINFO_URL, headers=headers)

    def create_or_update_user(self, backend_user):
        # Preferred username is not unique. Sub in UUID.
        username = f'keycloak_f{backend_user["sub"]}'
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
        username = backend_user["sub"]
        email = backend_user.get('email')
        first_name = backend_user['given_name']
        last_name = backend_user['family_name']
        # https://wiki.geant.org/display/eduTEAMS/Attributes+available+to+Relying+Parties#AttributesavailabletoRelyingParties-Assurance
        details = {
            'eduperson_assurance': backend_user.get('eduperson_assurance', []),
        }
        # https://wiki.geant.org/display/eduTEAMS/Attributes+available+to+Relying+Parties#AttributesavailabletoRelyingParties-AffiliationwithinHomeOrganization
        backend_affiliations = backend_user.get('voperson_external_affiliation', [])
        try:
            user = User.objects.get(username=username)
            update_fields = set()
            if user.details != details:
                user.details = details
                update_fields.add('details')
            if user.affiliations != backend_affiliations:
                user.affiliations = backend_affiliations
                update_fields.add('affiliations')
            if user.first_name != first_name:
                user.first_name = first_name
                update_fields.add('first_name')
            if user.last_name != last_name:
                user.last_name = last_name
                update_fields.add('last_name')
            if update_fields:
                user.save(update_fields=update_fields)
            created = False
        except User.DoesNotExist:
            created = True
            user = User.objects.create_user(
                username=username,
                registration_method=self.provider,
                email=email,
                first_name=first_name,
                last_name=last_name,
                details=details,
                affiliations=backend_affiliations,
            )
            user.set_unusable_password()
            user.save()

        existing_keys_map = {
            key.public_key: key
            for key in SshPublicKey.objects.filter(
                user=user, name__startswith='eduteams_'
            )
        }
        eduteams_keys = backend_user.get('ssh_public_key', [])

        new_keys = set(eduteams_keys) - set(existing_keys_map.keys())
        stale_keys = set(existing_keys_map.keys()) - set(eduteams_keys)

        for key in new_keys:
            name = 'eduteams_key_{}'.format(uuid.uuid4().hex[:10])
            new_key = SshPublicKey(user=user, name=name, public_key=key)
            new_key.save()

        for key in stale_keys:
            existing_keys_map[key].delete()

        return user, created


class RemoteEduteamsView(views.APIView):
    def post(self, request, *args, **kwargs):
        if not request.user.is_identity_manager:
            return Response(
                'Only identity manager is allowed to sync remote users.',
                status=status.HTTP_403_FORBIDDEN,
            )

        if (
            not self.get_token()
            or not self.get_userinfo_url()
            or not self.get_token_url()
        ):
            return Response(
                'Remote Eduteams user sync is disabled.',
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = RemoteEduteamsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cuid = serializer.validated_data['cuid']

        user = self.get_or_create_user(cuid)
        return Response({'uuid': user.uuid.hex})

    def get_token(self):
        return settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_ACCESS_TOKEN']

    def get_userinfo_url(self):
        return settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_USERINFO_URL']

    def get_token_url(self):
        return settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_TOKEN_URL']

    def get_or_create_user(self, username):
        try:
            return User.objects.get(
                username=username, registration_method=EduteamsView.provider
            )
        except User.DoesNotExist:
            user_info = self.get_user_info(username)
            return self.create_user(user_info)

    def create_user(self, user_info):
        user = User.objects.create_user(
            username=user_info['voperson_id'],
            registration_method=EduteamsView.provider,
            first_name=user_info['given_name'],
            last_name=user_info['family_name'],
            email=user_info['mail'][0],
        )
        for ssh_key in user_info.get('ssh_public_key', []):
            name = 'eduteams_key_{}'.format(uuid.uuid4().hex[:10])
            new_key = SshPublicKey(user=user, name=name, public_key=ssh_key)
            new_key.save()
        user.set_unusable_password()
        user.save()
        return user

    def get_user_info(self, cuid: str) -> dict:
        user_url = f'{self.get_userinfo_url()}/{cuid}'
        access_token = self.refresh_token()
        try:
            user_response = requests.get(
                user_url, headers={'Authorization': f'Bearer {access_token}'}
            )
        except requests.exceptions.RequestException as e:
            logger.warning('Unable to get Eduteams user info. Error is %s', e)
            raise OAuthException(self.provider, 'Unable to get user info.')

        if user_response.status_code != 200:
            raise OAuthException(self.provider, 'Unable to get user info.')

        try:
            return user_response.json()
        except (ValueError, TypeError):
            raise OAuthException(
                self.provider, 'Unable to parse JSON in user info response.'
            )

    def refresh_token(self):
        token_url = self.get_token_url()

        try:
            token_response = requests.post(
                token_url,
                auth=(EDUTEAMS_CLIENT_ID, EDUTEAMS_SECRET),
                data={
                    'grant_type': 'refresh_token',
                    'refresh_token': self.get_token(),
                    'scope': 'openid',
                },
            )
            if token_response.status_code != 200:
                raise OAuthException(
                    self.provider, 'Unable to get access token. Service is down.'
                )
            return token_response.json()['access_token']
        except requests.exceptions.RequestException as e:
            logger.warning('Unable to get Eduteams access token. Error is %s', e)
            raise OAuthException(self.provider, 'Unable to get access token.')


class RegistrationView(generics.CreateAPIView):
    permission_classes = ()
    authentication_classes = ()
    serializer_class = RegistrationSerializer

    @validate_local_signup
    def post(self, request, *args, **kwargs):
        return super(RegistrationView, self).post(request, *args, **kwargs)

    def perform_create(self, serializer):
        user = serializer.save()
        user.is_active = False
        user.save()
        transaction.on_commit(lambda: tasks.send_activation_email.delay(user.uuid.hex))


class ActivationView(views.APIView):
    permission_classes = ()
    authentication_classes = ()

    @validate_local_signup
    def post(self, request):
        serializer = ActivationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        serializer.user.is_active = True
        serializer.user.save()

        token = Token.objects.get(user=serializer.user)
        return Response({'token': token.key}, status=status.HTTP_201_CREATED)
