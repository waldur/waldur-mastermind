from __future__ import unicode_literals

from rest_framework import status
from rest_framework import test

from waldur_core.structure.tests.factories import UserFactory


class VersionApiPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.version_url = 'http://testserver/api/version/'

    def test_authenticated_user_can_access_version(self):
        user = UserFactory()
        self.client.force_authenticate(user=user)

        response = self.client.get(self.version_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_anonymous_user_can_access_version(self):
        response = self.client.get(self.version_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
