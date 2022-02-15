import json
from unittest import mock

import responses
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_auth_social.models import OAuthToken
from waldur_core.core.models import SshPublicKey
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure.tests import factories as structure_factories

User = get_user_model()


class BaseAuthTest(test.APITransactionTestCase):
    def setUp(self):
        self.valid_data = {
            'clientId': '4242324',
            'redirectUri': 'http://example.com/redirect/',
            'code': 'secret',
        }

    def oauth_login(self):
        responses.add(
            method='POST',
            url='http://keycloak/auth/realms/myrealm/protocol/openid-connect/token',
            json={'access_token': 'random_token', 'refresh_token': 'random_token'},
        )
        responses.add(
            method='GET',
            url='http://keycloak/auth/realms/myrealm/protocol/openid-connect/userinfo',
            json={'sub': '123', 'given_name': 'Alice', 'family_name': 'Lebowski'},
        )
        return self.client.post(reverse('auth_keycloak'), self.valid_data)


@override_settings(
    WALDUR_AUTH_SOCIAL={
        'KEYCLOAK_TOKEN_URL': 'http://keycloak/auth/realms/myrealm/protocol/openid-connect/token',
        'KEYCLOAK_USERINFO_URL': 'http://keycloak/auth/realms/myrealm/protocol/openid-connect/userinfo',
    }
)
@override_waldur_core_settings(AUTHENTICATION_METHODS=['SOCIAL_SIGNUP'])
@responses.activate
class SocialSignupTest(BaseAuthTest):
    def test_auth_view_works_for_anonymous_only(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.post(reverse('auth_keycloak'), self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_input_data_is_validated(self):
        response = self.client.post(reverse('auth_keycloak'), {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_if_auth_succeeded_user_and_profile_is_created(self):
        response = self.oauth_login()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            'Keycloak user', OAuthToken.objects.get(provider='keycloak').user.full_name
        )

    def test_expired_token_is_recreated_on_successful_authentication(self):
        response = self.oauth_login()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        token1 = response.data['token']

        lifetime = settings.WALDUR_CORE.get(
            'TOKEN_LIFETIME', timezone.timedelta(hours=1)
        )
        mocked_now = timezone.now() + lifetime
        with mock.patch('django.utils.timezone.now', lambda: mocked_now):
            response = self.oauth_login()
            token2 = response.data['token']
            self.assertNotEqual(token1, token2)

    @mock.patch('requests.post')
    def test_raises_exception_if_user_is_not_authorized(self, post_request_mock):
        invalid_response = {
            'error': 'invalid_client',
            'error_description': 'Unauthorized',
        }
        mockresponse = mock.Mock()
        post_request_mock.return_value = mockresponse
        mockresponse.text = json.dumps(invalid_response)

        response = self.client.post(reverse('auth_keycloak'), self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@override_waldur_core_settings(AUTHENTICATION_METHODS=['LOCAL_SIGNIN'])
class DisabledAuthenticationTest(BaseAuthTest):
    def test_auth_fails_if_social_authentication_is_not_enabled(self):
        response = self.oauth_login()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(b'Authentication method is disabled.' in response.content)


@override_waldur_core_settings(AUTHENTICATION_METHODS=['SOCIAL_SIGNUP'])
@override_settings(
    WALDUR_AUTH_SOCIAL={
        'EDUTEAMS_TOKEN_URL': 'https://proxy.acc.eduteams.org/OIDC/token',
        'EDUTEAMS_USERINFO_URL': 'https://proxy.acc.eduteams.org/OIDC/userinfo',
    }
)
@responses.activate
class EduteamsAuthenticationTest(test.APITransactionTestCase):
    def setUp(self):
        super(EduteamsAuthenticationTest, self).setUp()
        self.valid_data = {
            'clientId': '4242324',
            'redirectUri': 'http://example.com/redirect/',
            'code': 'secret',
        }
        self.backend_user = {
            'name': 'Jack Dougherty',
            'given_name': 'Jack',
            'family_name': 'Dougherty',
            'email': 'jack.dougherty@example.com',
            'sub': '28c5353b8bb34984a8bd4169ba94c606@eduteams.org',
            'eduperson_assurance': [
                'https://refeds',
                'https://refeds/ID/unique',
                'https://refeds/ID/eppn-unique-no-reassign',
                'https://refeds/IAP/low',
                'https://refeds$/ATP/ePA-1m',
                'https://refeds/ATP/ePA-1d',
            ],
            'ssh_public_key': [
                'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHaD5EERMoDJvjH9p4wR19MFX6y+VI6J6432cI5x4PjT'
            ],
            'voperson_external_affiliation': ['faculty@helsinki.fi'],
        }
        responses.add(
            method='POST',
            url='https://proxy.acc.eduteams.org/OIDC/token',
            json={"access_token": "random_token", 'refresh_token': 'random_token'},
        )

    def test_details_are_imported(self):
        responses.add(
            method='GET',
            url='https://proxy.acc.eduteams.org/OIDC/userinfo',
            json=self.backend_user,
        )

        response = self.client.post(reverse('auth_eduteams'), self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(
            username='28c5353b8bb34984a8bd4169ba94c606@eduteams.org'
        )
        self.assertEqual(user.email, self.backend_user['email'])
        self.assertEqual(user.full_name, self.backend_user['name'])
        self.assertEqual(
            user.details,
            {'eduperson_assurance': self.backend_user['eduperson_assurance']},
        )
        ssh_key = SshPublicKey.objects.get(user=user)
        self.assertEqual(ssh_key.public_key, self.backend_user['ssh_public_key'][0])
        self.assertTrue(ssh_key.name.startswith('eduteams_'))
        self.assertEqual(user.affiliations, ['faculty@helsinki.fi'])

    def test_invalid_ssh_keys_are_ignored_valid_are_saved(self):
        self.backend_user['ssh_public_key'].append('THIS_IS_INVALID_SSH_KEY')

        responses.add(
            method='GET',
            url='https://proxy.acc.eduteams.org/OIDC/userinfo',
            json=self.backend_user,
        )

        response = self.client.post(reverse('auth_eduteams'), self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(
            username='28c5353b8bb34984a8bd4169ba94c606@eduteams.org'
        )
        ssh_key = SshPublicKey.objects.get(user=user)
        self.assertEqual(ssh_key.public_key, self.backend_user['ssh_public_key'][0])


@override_settings(
    WALDUR_AUTH_SOCIAL={
        'REMOTE_EDUTEAMS_REFRESH_TOKEN': '28c5353b8bb34984a8bd4169ba94c606',
        'REMOTE_EDUTEAMS_USERINFO_URL': 'https://proxy.acc.researcher-access.org/api/userinfo',
        'REMOTE_EDUTEAMS_TOKEN_URL': 'https://proxy.acc.researcher-access.org/OIDC/token',
        'REMOTE_EDUTEAMS_CLIENT_ID': 'WaldurId',
        'REMOTE_EDUTEAMS_SECRET': 'WaldurSecret',
        'REMOTE_EDUTEAMS_ENABLED': True,
    }
)
class RemoteEduteamsTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super(RemoteEduteamsTest, self).setUp()
        self.url = reverse('auth_remote_eduteams')
        self.valid_cuid = (
            '87b867ff52768f8c11f1501598c2dd1e526fe7f0@acc.researcher-access.org'
        )
        self.user_url = (
            f'https://proxy.acc.researcher-access.org/api/userinfo/{self.valid_cuid}'
        )
        responses.add(
            method='POST',
            url='https://proxy.acc.researcher-access.org/OIDC/token',
            json={"access_token": "random_token"},
        )

    def setup_user_info(self):
        responses.add(
            method='GET',
            url=self.user_url,
            json={
                "voperson_id": "87b867ff52768f8c11f1501598c2dd1e526fe7f0@acc.researcher-access.org",
                "name": "John Snow",
                "given_name": "John",
                "family_name": "Snow",
                "mail": ["john@snow.me"],
                "ssh_public_key": [
                    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHaD5EERMoDJvjH9p4wR19MFX6y+VI6J6432cI5x4PjT'
                ],
            },
        )

    def test_unauthorized_user_can_not_sync_remote_users(self):
        self.setup_user_info()
        user = structure_factories.UserFactory()
        self.client.force_login(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data,
            'Only staff and identity manager are allowed to sync remote users.',
        )

    @responses.activate
    def test_when_user_does_not_exist_remote_api_is_called(self):
        self.setup_user_info()
        user = structure_factories.UserFactory(is_identity_manager=True)
        self.client.force_login(user)

        response = self.client.post(self.url, {'cuid': self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        new_user = User.objects.get(username=self.valid_cuid)
        self.assertEqual(new_user.email, "john@snow.me")
        self.assertEqual(new_user.full_name, "John Snow")

        keys = SshPublicKey.objects.filter(user=new_user)
        self.assertEqual(keys.count(), 1)

    @responses.activate
    def test_staff_can_trigger_remote_user_sync(self):
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        response = self.client.post(self.url, {'cuid': self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @responses.activate
    def test_when_user_exists_it_is_updated(self):
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        remote_user = structure_factories.UserFactory(
            username=self.valid_cuid, email='foo@example.com'
        )

        response = self.client.post(self.url, {'cuid': self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        remote_user.refresh_from_db()
        self.assertEqual(remote_user.email, "john@snow.me")
        self.assertEqual(remote_user.full_name, "John Snow")

        keys = SshPublicKey.objects.filter(user=remote_user)
        self.assertEqual(keys.count(), 1)

    @responses.activate
    @mock.patch('waldur_core.core.handlers.event_logger')
    def test_when_user_is_updated_events_are_emitted(self, mock_event_logger):
        self.setup_user_info()
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        structure_factories.UserFactory(
            first_name='Steve',
            last_name='Jobs',
            username=self.valid_cuid,
            email='steve@jobs.com',
        )

        self.client.post(self.url, {'cuid': self.valid_cuid})

        msg = mock_event_logger.user.info.call_args[0][0]
        test_msg = (
            'User {affected_user_username} has been updated. Details:\n'
            'email: steve@jobs.com -> john@snow.me\n'
            'first_name: Steve -> John\n'
            'last_name: Jobs -> Snow'
        )
        self.assertEqual(test_msg, msg)

    @responses.activate
    def test_when_user_is_not_found_it_is_disabled(self):
        responses.add(
            method='GET',
            url=self.user_url,
            status=404,
        )

        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_login(user)

        remote_user = structure_factories.UserFactory(
            username=self.valid_cuid, email='foo@example.com'
        )

        response = self.client.post(self.url, {'cuid': self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        remote_user.refresh_from_db()
        self.assertFalse(remote_user.is_active)
