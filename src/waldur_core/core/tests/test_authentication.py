from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test, status
from rest_framework.authtoken.models import Token

from . import helpers


class TokenAuthenticationTest(test.APITransactionTestCase):
    def setUp(self):
        self.username = 'test'
        self.password = 'secret'
        self.auth_url = 'http://testserver' + reverse('auth-password')
        self.test_url = 'http://testserver/api/'
        get_user_model().objects.create_user(self.username, 'admin@example.com', self.password)

    def tearDown(self):
        cache.clear()

    def test_user_can_authenticate_with_token(self):
        response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        token = response.data['token']
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + token)
        response = self.client.get(self.test_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_token_expires_based_on_user_token_lifetime(self):
        user = get_user_model().objects.get(username=self.username)
        configured_token_lifetime = settings.WALDUR_CORE.get('TOKEN_LIFETIME', timezone.timedelta(hours=1))
        user_token_lifetime = configured_token_lifetime - timezone.timedelta(seconds=40)
        user.token_lifetime = user_token_lifetime.seconds
        user.save()

        response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        token = response.data['token']
        mocked_now = timezone.now() + user_token_lifetime
        with freeze_time(mocked_now):
            self.client.credentials(HTTP_AUTHORIZATION='Token ' + token)
            response = self.client.get(self.test_url)
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(response.data['detail'], 'Token has expired.')

    def test_token_creation_time_is_updated_on_every_request(self):
        response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        token = response.data['token']
        created1 = Token.objects.values_list('created', flat=True).get(key=token)

        self.client.credentials(HTTP_AUTHORIZATION='Token ' + token)
        self.client.get(self.test_url)
        created2 = Token.objects.values_list('created', flat=True).get(key=token)
        self.assertTrue(created1 < created2)

    def test_account_is_blocked_after_five_failed_attempts(self):
        for _ in range(5):
            response = self.client.post(self.auth_url, data={'username': self.username, 'password': 'WRONG'})
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # this one should fail with a different error message
        self.client.post(self.auth_url, data={'username': self.username, 'password': 'WRONG'})
        self.assertEqual(response.data['detail'], 'Username is locked out. Try in 10 minutes.')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_expired_token_is_recreated_on_successful_authentication(self):
        user = get_user_model().objects.get(username=self.username)
        self.assertIsNotNone(user.token_lifetime)
        response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        token1 = response.data['token']

        mocked_now = timezone.now() + timezone.timedelta(seconds=user.token_lifetime)
        with freeze_time(mocked_now):
            response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
            token2 = response.data['token']
            self.assertNotEqual(token1, token2)

    def test_not_expired_token_creation_time_is_updated_on_authentication(self):
        response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        token1 = response.data['token']
        created1 = Token.objects.values_list('created', flat=True).get(key=token1)

        response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
        token2 = response.data['token']
        created2 = Token.objects.values_list('created', flat=True).get(key=token2)

        self.assertEqual(token1, token2)
        self.assertTrue(created1 < created2)

    def test_token_never_expires_if_token_lifetime_is_none(self):
        user = get_user_model().objects.get(username=self.username)
        user.token_lifetime = None
        user.save()

        response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        original_token = response.data['token']

        year_ahead = timezone.now() + timezone.timedelta(days=365)
        with freeze_time(year_ahead):
            response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
            token_in_a_year = response.data['token']
            self.assertEqual(original_token, token_in_a_year)

    def test_token_created_date_is_refreshed_even_if_token_lifetime_is_none(self):
        response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user = get_user_model().objects.get(username=self.username)
        original_token_lifetime = user.token_lifetime
        original_created_value = user.auth_token.created
        user.token_lifetime = None
        user.save()

        last_refresh_time = timezone.now() + timezone.timedelta(seconds=original_token_lifetime)
        with freeze_time(last_refresh_time):
            response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            token = response.data['token']

        user.auth_token.refresh_from_db()
        self.assertTrue(user.auth_token.created > original_created_value)

        user.token_lifetime = original_token_lifetime
        user.save()
        with freeze_time(last_refresh_time):
            self.client.credentials(HTTP_AUTHORIZATION='Token ' + token)
            response = self.client.get(self.test_url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    @helpers.override_waldur_core_settings(AUTHENTICATION_METHODS=[])
    def test_authentication_fails_if_local_signin_is_disabled(self):
        response = self.client.post(self.auth_url, data={'username': self.username, 'password': self.password})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(b'Authentication method is disabled.' in response.content)
