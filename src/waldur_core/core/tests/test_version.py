from unittest import mock

from constance.test.unittest import override_config
from rest_framework import status, test

from waldur_core.structure.tests.factories import UserFactory


class VersionApiPermissionTest(test.APITestCase):
    def setUp(self):
        self.user = UserFactory()
        self.version_url = "http://testserver/api/version/"

    def test_authenticated_user_can_access_version(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.version_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("version", response.data)
        self.assertNotIn("latest_version", response.data)

    def test_anonymous_user_can_not_access_version(self):
        response = self.client.get(self.version_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class VersionUpdateCheckTest(test.APITestCase):
    def setUp(self):
        self.staff = UserFactory(is_staff=True)
        self.support = UserFactory(is_support=True)
        self.regular_user = UserFactory()
        self.version_url = "http://testserver/api/version/"

    @override_config(CHECK_FOR_UPDATES=False)
    @mock.patch("waldur_core.core.views.requests.get")
    def test_no_github_request_when_update_check_disabled(self, mock_get):
        self.client.force_authenticate(user=self.staff)

        response = self.client.get(self.version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("latest_version", response.data)
        mock_get.assert_not_called()

    @override_config(CHECK_FOR_UPDATES=True)
    @mock.patch("waldur_core.core.views.cache")
    @mock.patch("waldur_core.core.views.requests.get")
    def test_staff_gets_latest_version_when_update_check_enabled(
        self, mock_get, mock_cache
    ):
        mock_cache.get.return_value = None
        mock_get.return_value.json.return_value = [
            {"ref": "refs/tags/1.0.0"},
            {"ref": "refs/tags/2.0.0"},
        ]
        self.client.force_authenticate(user=self.staff)

        response = self.client.get(self.version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["latest_version"], "2.0.0")
        mock_get.assert_called_once()

    @override_config(CHECK_FOR_UPDATES=True)
    @mock.patch("waldur_core.core.views.cache")
    @mock.patch("waldur_core.core.views.requests.get")
    def test_support_gets_latest_version_when_update_check_enabled(
        self, mock_get, mock_cache
    ):
        mock_cache.get.return_value = None
        mock_get.return_value.json.return_value = [
            {"ref": "refs/tags/1.0.0"},
            {"ref": "refs/tags/2.0.0"},
        ]
        self.client.force_authenticate(user=self.support)

        response = self.client.get(self.version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["latest_version"], "2.0.0")
        mock_get.assert_called_once()

    @override_config(CHECK_FOR_UPDATES=True)
    @mock.patch("waldur_core.core.views.requests.get")
    def test_regular_user_does_not_trigger_github_request(self, mock_get):
        self.client.force_authenticate(user=self.regular_user)

        response = self.client.get(self.version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("version", response.data)
        self.assertNotIn("latest_version", response.data)
        mock_get.assert_not_called()
