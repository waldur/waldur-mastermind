import json
from unittest import mock

import responses
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_auth_social.models import AuthProfile
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

    def facebook_login(self):
        with mock.patch(
            'waldur_auth_social.views.FacebookView.get_backend_user'
        ) as get_backend_user:
            get_backend_user.return_value = {'id': '123', 'name': 'Facebook user'}
            return self.client.post(reverse('auth_facebook'), self.valid_data)


@override_waldur_core_settings(AUTHENTICATION_METHODS=['SOCIAL_SIGNUP'])
class SocialSignupTest(BaseAuthTest):
    def test_auth_view_works_for_anonymous_only(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)
        response = self.client.post(reverse('auth_facebook'), self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_input_data_is_validated(self):
        response = self.client.post(reverse('auth_facebook'), {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_if_user_already_exists_it_is_not_created_again(self):
        user = structure_factories.UserFactory()
        user.auth_profile.facebook = '123'
        user.auth_profile.save()

        response = self.facebook_login()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_if_facebook_auth_succeeded_user_and_profile_is_created(self):
        response = self.facebook_login()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            'Facebook user', AuthProfile.objects.get(facebook='123').user.full_name
        )

    def test_expired_token_is_recreated_on_successful_authentication(self):
        response = self.facebook_login()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        token1 = response.data['token']

        lifetime = settings.WALDUR_CORE.get(
            'TOKEN_LIFETIME', timezone.timedelta(hours=1)
        )
        mocked_now = timezone.now() + lifetime
        with mock.patch('django.utils.timezone.now', lambda: mocked_now):
            response = self.facebook_login()
            token2 = response.data['token']
            self.assertNotEqual(token1, token2)

    @mock.patch('requests.post')
    def test_raises_exception_if_user_is_not_authorized_by_facebook(
        self, post_request_mock
    ):
        invalid_response = {
            'error': 'invalid_client',
            'error_description': 'Unauthorized',
        }
        mockresponse = mock.Mock()
        post_request_mock.return_value = mockresponse
        mockresponse.text = json.dumps(invalid_response)

        response = self.client.post(reverse('auth_facebook'), self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class LocalSignupTest(test.APITransactionTestCase):
    def post_request(self):
        return self.client.post(
            reverse('auth_registration'),
            {
                'username': 'alice2018',
                'full_name': 'Alice Lebowski',
                'email': 'alice@example.com',
                'password': 'secret',
            },
        )

    @override_waldur_core_settings(AUTHENTICATION_METHODS=['LOCAL_SIGNUP'])
    def test_local_signup_creates_user(self):
        response = self.post_request()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_waldur_core_settings(AUTHENTICATION_METHODS=['LOCAL_SIGNIN'])
    def test_local_signup_fails_if_it_is_not_enabled(self):
        response = self.post_request()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(b'Authentication method is disabled.' in response.content)

    @override_waldur_core_settings(AUTHENTICATION_METHODS=['LOCAL_SIGNUP'])
    @override_settings(task_always_eager=True)
    def test_activation_email_is_sent(self):
        response = self.post_request()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(response.data['email'], mail.outbox[0].to[0])


@override_waldur_core_settings(AUTHENTICATION_METHODS=['LOCAL_SIGNIN'])
class DisabledAuthenticationTest(BaseAuthTest):
    def test_facebook_auth_fails_if_social_authentication_is_not_enabled(self):
        response = self.facebook_login()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(b'Authentication method is disabled.' in response.content)


@override_waldur_core_settings(AUTHENTICATION_METHODS=['SOCIAL_SIGNUP'])
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
                'ssh-ed25519 AAAAC3NqaC1lZDI1TTE5AAAAIJ4pfKk7hRdUVeMfrKdLYhxdKy92nVPuHDlVVvZMyqeP'
            ],
            'voperson_external_affiliation': ['faculty@helsinki.fi'],
        }

    def test_details_are_imported(self):
        with mock.patch(
            'waldur_auth_social.views.EduteamsView.get_backend_user'
        ) as get_backend_user:
            get_backend_user.return_value = self.backend_user
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


@override_settings(
    WALDUR_AUTH_SOCIAL={
        'REMOTE_EDUTEAMS_ACCESS_TOKEN': '28c5353b8bb34984a8bd4169ba94c606',
        'REMOTE_EDUTEAMS_USERINFO_URL': 'https://proxy.acc.researcher-access.org/api/userinfo',
        'REMOTE_EDUTEAMS_TOKEN_URL': 'https://proxy.acc.researcher-access.org/OIDC/token',
    }
)
class RemoteEduteamsTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super(RemoteEduteamsTest, self).setUp()
        self.url = reverse('auth_remote_eduteams')
        self.valid_cuid = (
            '87b867ff52768f8c11f1501598c2dd1e526fe7f0@acc.researcher-access.org'
        )

    def test_unauthorized_user_can_not_sync_remote_users(self):
        user = structure_factories.UserFactory()
        self.client.force_login(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data, 'Only identity manager is allowed to sync remote users.'
        )

    def test_when_user_already_exists_local_uuid_is_returned(self):
        eduteams_user = structure_factories.UserFactory(
            registration_method='eduteams',
            username=self.valid_cuid,
            email='john@snow.me',
        )
        user = structure_factories.UserFactory(is_identity_manager=True)
        self.client.force_login(user)
        response = self.client.post(self.url, {'cuid': eduteams_user.username})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['uuid'], eduteams_user.uuid.hex)

    @responses.activate
    def test_when_user_does_not_exist_remote_api_is_called(self):
        user_url = (
            f'https://proxy.acc.researcher-access.org/api/userinfo/{self.valid_cuid}'
        )
        responses.add(
            method='POST',
            url='https://proxy.acc.researcher-access.org/OIDC/token',
            json={"access_token": "random_token"},
        )
        responses.add(
            method='GET',
            url=user_url,
            json={
                "voperson_id": "87b867ff52768f8c11f1501598c2dd1e526fe7f0@acc.researcher-access.org",
                "name": "John Snow",
                "given_name": "John",
                "family_name": "Snow",
                "mail": ["john@snow.me"],
                "ssh_public_key": [
                    'ssh-ed25519 AAAAC3NqaC1lZDI1TTE5AAAAIJ4pfKk7hRdUVeMfrKdLYhxdKy92nVPuHDlVVvZMyqeP'
                ],
            },
        )
        user = structure_factories.UserFactory(is_identity_manager=True)
        self.client.force_login(user)

        response = self.client.post(self.url, {'cuid': self.valid_cuid})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        new_user = User.objects.get(username=self.valid_cuid)
        self.assertEqual(new_user.email, "john@snow.me")
        self.assertEqual(new_user.full_name, "John Snow")

        keys = SshPublicKey.objects.filter(user=new_user)
        self.assertEqual(keys.count(), 1)
