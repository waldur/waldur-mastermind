import json
from unittest import mock

from django.conf import settings
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure.tests import factories as structure_factories

from ..models import AuthProfile


class BaseAuthTest(test.APITransactionTestCase):
    def setUp(self):
        self.valid_data = {
            'clientId': '4242324',
            'redirectUri': 'http://example.com/redirect/',
            'code': 'secret',
        }

    def google_login(self):
        with mock.patch(
            'waldur_auth_social.views.GoogleView.get_backend_user'
        ) as get_backend_user:
            get_backend_user.return_value = {'id': '123', 'name': 'Google user'}
            return self.client.post(reverse('auth_google'), self.valid_data)

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
        response = self.client.post(reverse('auth_google'), self.valid_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_input_data_is_validated(self):
        response = self.client.post(reverse('auth_google'), {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_if_google_auth_succeeded_user_and_profile_is_created(self):
        response = self.google_login()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            'Google user', AuthProfile.objects.get(google='123').user.full_name
        )

    def test_if_user_already_exists_it_is_not_created_again(self):
        user = structure_factories.UserFactory()
        user.auth_profile.google = '123'
        user.auth_profile.save()

        response = self.google_login()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_if_facebook_auth_succeeded_user_and_profile_is_created(self):
        response = self.facebook_login()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            'Facebook user', AuthProfile.objects.get(facebook='123').user.full_name
        )

    def test_expired_token_is_recreated_on_successful_authentication(self):
        response = self.google_login()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        token1 = response.data['token']

        lifetime = settings.WALDUR_CORE.get(
            'TOKEN_LIFETIME', timezone.timedelta(hours=1)
        )
        mocked_now = timezone.now() + lifetime
        with mock.patch('django.utils.timezone.now', lambda: mocked_now):
            response = self.google_login()
            token2 = response.data['token']
            self.assertNotEqual(token1, token2)

    @mock.patch('requests.post')
    def test_raises_exception_if_user_is_not_authorized_by_google(
        self, post_request_mock
    ):
        invalid_response = {
            'error': 'invalid_client',
            'error_description': 'Unauthorized',
        }
        mockresponse = mock.Mock()
        post_request_mock.return_value = mockresponse
        mockresponse.text = json.dumps(invalid_response)

        response = self.client.post(reverse('auth_google'), self.valid_data)
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
    def test_google_auth_fails_if_social_authentication_is_not_enabled(self):
        response = self.google_login()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(b'Authentication method is disabled.' in response.content)
