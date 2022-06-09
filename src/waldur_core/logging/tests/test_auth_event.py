from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status, test

from waldur_core.logging import models


class AuthenticationEventLogTest(test.APITransactionTestCase):
    def setUp(self):
        self.username = 'test'
        self.password = 'secret'
        self.auth_url = 'http://testserver' + reverse('auth-password')
        get_user_model().objects.create_user(
            self.username, 'admin@example.com', self.password
        )

    def tearDown(self):
        cache.clear()

    def test_add_auth_info_to_context(self):
        response = self.client.post(
            self.auth_url, data={'username': self.username, 'password': self.password}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        event = models.Event.objects.get(event_type='auth_logged_in_with_username')
        self.assertTrue('user_agent' in event.context.keys())
