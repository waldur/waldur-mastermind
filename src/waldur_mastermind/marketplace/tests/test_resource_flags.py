from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class ResourceFlagsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

    def _get_url(self, action):
        return factories.ResourceFactory.get_url(self.resource, action)

    def _make_request(self, action, payload, user="staff"):
        url = self._get_url(action)
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(url, payload)

    @data("set_downscaled", "set_paused", "set_restrict_member_access")
    def test_staff_can_set_flags(self, action):
        field_map = {
            "set_downscaled": "downscaled",
            "set_paused": "paused",
            "set_restrict_member_access": "restrict_member_access",
        }
        field = field_map[action]

        # Test setting to True
        response = self._make_request(action, {field: True}, "staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertTrue(getattr(self.resource, field))

        # Test setting to False
        response = self._make_request(action, {field: False}, "staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertFalse(getattr(self.resource, field))

    @data("set_downscaled", "set_paused", "set_restrict_member_access")
    def test_non_staff_cannot_set_flags(self, action):
        field_map = {
            "set_downscaled": "downscaled",
            "set_paused": "paused",
            "set_restrict_member_access": "restrict_member_access",
        }
        field = field_map[action]

        # Test as owner
        response = self._make_request(action, {field: True}, "owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # Test as admin
        response = self._make_request(action, {field: True}, "admin")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("set_downscaled", "set_paused", "set_restrict_member_access")
    def test_no_change_returns_not_changed(self, action):
        field_map = {
            "set_downscaled": "downscaled",
            "set_paused": "paused",
            "set_restrict_member_access": "restrict_member_access",
        }
        field = field_map[action]

        # Get current value
        current_value = getattr(self.resource, field)

        # Set to same value
        response = self._make_request(action, {field: current_value}, "staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("not changed", response.data["status"])
