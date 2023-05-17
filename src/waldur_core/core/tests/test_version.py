from rest_framework import status, test

from waldur_core.structure.tests.factories import UserFactory


class VersionApiPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = UserFactory(is_staff=True)
        self.support = UserFactory(is_support=True)
        self.regular_user = UserFactory()
        self.version_url = 'http://testserver/api/version/'

    def test_staff_can_access_version(self):
        self.client.force_authenticate(user=self.staff)

        response = self.client.get(self.version_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_can_access_version(self):
        self.client.force_authenticate(user=self.support)

        response = self.client.get(self.version_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_regular_user_can_not_access_version(self):
        self.client.force_authenticate(user=self.regular_user)

        response = self.client.get(self.version_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_can_not_access_version(self):
        response = self.client.get(self.version_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
