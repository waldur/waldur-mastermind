from ddt import data, ddt
from rest_framework import status, test
from rest_framework.authtoken import models as authtoken_models

from waldur_core.structure.tests import factories, fixtures


@ddt
class TokenListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.url = factories.AuthTokenFactory.get_list_url()

    def test_staff_can_list_tokens(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)

    @data('user', 'global_support')
    def test_user_can_not_get_list_tokens(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


@ddt
class TokenDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.url = factories.AuthTokenFactory.get_list_url()

    def test_staff_can_delete_token(self):
        self.client.force_authenticate(user=self.fixture.user)
        self.client.force_authenticate(user=self.fixture.staff)
        token = authtoken_models.Token.objects.get(user=self.fixture.user)
        url = factories.AuthTokenFactory.get_url(token)
        response = self.client.delete(url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)

    @data('user', 'global_support')
    def test_user_can_not_delete_token(self, user):
        self.client.force_authenticate(user=self.fixture.staff)
        self.client.force_authenticate(user=getattr(self.fixture, user))
        token = authtoken_models.Token.objects.get(user=self.fixture.staff)
        url = factories.AuthTokenFactory.get_url(token)
        response = self.client.delete(url)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)
