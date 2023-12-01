import logging
import uuid

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone
from requests.auth import HTTPBasicAuth
from rest_framework.exceptions import NotFound, ParseError

from waldur_auth_social.exceptions import OAuthException
from waldur_auth_social.models import ProviderChoices
from waldur_core.core.models import SshPublicKey
from waldur_core.core.validators import validate_ssh_public_key

User = get_user_model()


logger = logging.getLogger(__name__)


def create_or_update_oauth_user(provider, backend_user):
    if provider == ProviderChoices.TARA:
        return create_or_update_tara_user(backend_user)
    if provider == ProviderChoices.EDUTEAMS:
        return create_or_update_eduteams_user(backend_user)
    if provider == ProviderChoices.KEYCLOAK:
        return create_or_update_keycloak_user(backend_user)


def generate_username():
    return uuid.uuid4().hex[:30]


def create_or_update_tara_user(backend_user):
    """
    See also reference documentation for TARA authentication in Estonian language:
    https://e-gov.github.io/TARA-Doku/TehnilineKirjeldus#431-identsust%C3%B5end
    """

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
        raise OAuthException(
            ProviderChoices.TARA, 'Unable to parse identity certificate.'
        )
    try:
        user = User.objects.get(civil_number=civil_number)
    except User.DoesNotExist:
        created = True
        user = User.objects.create_user(
            username=generate_username(),
            first_name=first_name,
            last_name=last_name,
            civil_number=civil_number,
            registration_method=ProviderChoices.TARA,
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


def create_or_update_keycloak_user(backend_user):
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
            registration_method=ProviderChoices.KEYCLOAK,
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
        if user.email != email:
            user.email = email
            update_fields.add('email')
        if update_fields:
            user.save(update_fields=update_fields)
    return user, created


def sync_user_ssh_keys(user, eduteams_keys, username):
    existing_keys_map = {
        key.public_key: key
        for key in SshPublicKey.objects.filter(user=user, name__startswith='eduteams_')
    }

    new_keys = set(eduteams_keys) - set(existing_keys_map.keys())
    stale_keys = set(existing_keys_map.keys()) - set(eduteams_keys)

    for key in new_keys:
        try:
            validate_ssh_public_key(key)
        except ValidationError:
            logger.debug(
                'Skipping invalid SSH key synchronization for remote eduTEAMS user %s',
                username,
            )
            continue
        name = f'eduteams_key_{uuid.uuid4().hex[:10]}'
        new_key = SshPublicKey(user=user, name=name, public_key=key)
        new_key.save()
        logger.info('%s key is added to user %s', new_key)

    for key in stale_keys:
        logger.info(
            'Deleting stale keys for user %s. Keys: ',
            username,
            ', '.join([key for key in stale_keys]),
        )
        existing_keys_map[key].delete()


def create_or_update_eduteams_user(backend_user):
    username = backend_user.get('sub') or backend_user.get('voperson_id')
    email = backend_user.get('email')
    if backend_user.get('mail'):
        email = backend_user['mail'][0]
    first_name = backend_user['given_name']
    last_name = backend_user['family_name']
    # https://wiki.geant.org/display/eduTEAMS/Attributes+available+to+Relying+Parties#AttributesavailabletoRelyingParties-Assurance
    details = {
        'eduperson_assurance': backend_user.get('eduperson_assurance', []),
    }
    # https://wiki.geant.org/display/eduTEAMS/Attributes+available+to+Relying+Parties#AttributesavailabletoRelyingParties-AffiliationwithinHomeOrganization
    backend_affiliations = backend_user.get('voperson_external_affiliation', [])
    payload = {
        'details': details,
        'affiliations': backend_affiliations,
        'first_name': first_name,
        'last_name': last_name,
        'email': email,
    }
    try:
        user = User.objects.get(username=username)
        user.last_sync = timezone.now()
        update_fields = set(['last_sync'])
        for key, value in payload.items():
            if getattr(user, key) != value:
                setattr(user, key, value)
                update_fields.add(key)
        user.save(update_fields=update_fields)
        created = False
    except User.DoesNotExist:
        created = True
        user = User.objects.create_user(
            username=username,
            registration_method=ProviderChoices.EDUTEAMS,
            notifications_enabled=False,
            **payload,
        )
        user.set_unusable_password()
        user.save()
    eduteams_keys = backend_user.get('ssh_public_key', [])

    sync_user_ssh_keys(user, eduteams_keys, username)

    return user, created


def pull_remote_eduteams_user(username):
    try:
        user_info = get_remote_eduteams_user_info(username)
    except NotFound:
        try:
            user = User.objects.get(username=username, is_active=True)
        except User.DoesNotExist:
            return
        else:
            user.is_active = False
            user.last_sync = timezone.now()
            user.save(update_fields=['is_active', 'last_sync'])
    else:
        user, _ = create_or_update_eduteams_user(user_info)
    return user


def get_remote_eduteams_user_info(username):
    user_url = (
        f'{settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_USERINFO_URL"]}/{username}'
    )
    access_token = refresh_remote_eduteams_token()
    try:
        user_response = requests.get(
            user_url, headers={'Authorization': f'Bearer {access_token}'}
        )
    except requests.exceptions.RequestException as e:
        logger.warning('Unable to get eduTEAMS user info. Error is %s', e)
        raise ParseError('Unable to get user info for %s' % user_url)

    if user_response.status_code == 404:
        raise NotFound('User %s does not exist' % user_url)

    if user_response.status_code != 200:
        raise ParseError('Unable to get user info for %s' % user_url)

    try:
        return user_response.json()
    except (ValueError, TypeError):
        raise ParseError('Unable to parse JSON in user info response.')


def get_remote_eduteams_ssh_keys():
    ssh_api_url = settings.WALDUR_AUTH_SOCIAL.get('REMOTE_EDUTEAMS_SSH_API_URL')
    if not ssh_api_url:
        logger.warning('REMOTE_EDUTEAMS_SSH_API_URL is empty')
        return

    ssh_api_username = settings.WALDUR_AUTH_SOCIAL.get(
        "REMOTE_EDUTEAMS_SSH_API_USERNAME"
    )
    if not ssh_api_username:
        logger.warning('REMOTE_EDUTEAMS_SSH_API_USERNAME is empty')
        return

    ssh_api_password = settings.WALDUR_AUTH_SOCIAL.get(
        "REMOTE_EDUTEAMS_SSH_API_PASSWORD"
    )
    if not ssh_api_password:
        logger.warning('REMOTE_EDUTEAMS_SSH_API_PASSWORD is empty')
        return

    ssh_api_endpoint = f"{ssh_api_url}/api/vo/puhuri/ssh_keys"

    try:
        basic_auth = HTTPBasicAuth(ssh_api_username, ssh_api_password)
        response = requests.get(ssh_api_endpoint, auth=basic_auth)
    except requests.exceptions.RequestException as e:
        logger.warning('Unable to get eduTEAMS ssh keys. Error is %s', e)
        raise ParseError('Unable to get eduTEAMS ssh keys for %s' % ssh_api_endpoint)

    if response.status_code != 200:
        raise ParseError('Unable to get eduTEAMS ssh keys for %s' % ssh_api_endpoint)

    try:
        ssh_keys_json = response.json()
        ssh_keys_list = ssh_keys_json['data']
        return ssh_keys_list
    except (ValueError, TypeError) as exc:
        raise ParseError('Unable to parse JSON in user info response: %s' % exc)


def refresh_remote_eduteams_token():
    access_token = cache.get('REMOTE_EDUTEAMS_ACCESS_TOKEN')
    if access_token:
        return access_token
    try:
        token_response = requests.post(
            settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_TOKEN_URL'],
            auth=(
                settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_CLIENT_ID'],
                settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_SECRET'],
            ),
            data={
                'grant_type': 'refresh_token',
                'refresh_token': settings.WALDUR_AUTH_SOCIAL[
                    'REMOTE_EDUTEAMS_REFRESH_TOKEN'
                ],
                'scope': 'openid',
            },
        )
        if token_response.status_code != 200:
            raise ParseError('Unable to get access token. Service is down.')
        try:
            access_token = token_response.json()['access_token']
            cache.set('REMOTE_EDUTEAMS_ACCESS_TOKEN', access_token, 30 * 60)
            return access_token
        except (ValueError, TypeError):
            raise ParseError('Unable to parse JSON in access token response.')
    except requests.exceptions.RequestException as e:
        logger.warning('Unable to get eduTEAMS access token. Error is %s', e)
        raise ParseError('Unable to get access token.')
