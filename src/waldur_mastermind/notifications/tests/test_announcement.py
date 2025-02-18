from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.notifications.tests import factories


class AdminAnnouncementCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.url = factories.AdminAnnouncementFactory.get_list_url()
        self.payload = {
            "description": "admin announcement",
            "type": "information",
            "active_to": "2026-01-02T00:00:00Z",
        }

    @freeze_time("2025-01-01")
    def test_staff_can_create_admin_announcement(self):
        payload_for_active = self.payload.copy()
        payload_for_active["active_from"] = "2025-01-01T00:00:00Z"
        response = self.create_admin_announcement(
            self.fixture.staff, payload_for_active
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["description"], "admin announcement")
        self.assertEqual(response.data["type"], "information")
        self.assertTrue(response.data["is_active"])

        payload_for_inactive = self.payload.copy()
        payload_for_inactive["active_from"] = "2025-01-02T00:00:00Z"
        response = self.create_admin_announcement(
            self.fixture.staff, payload_for_inactive
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["description"], "admin announcement")
        self.assertEqual(response.data["type"], "information")
        self.assertFalse(response.data["is_active"])

    def test_user_cannot_create_admin_announcement(self):
        payload = {
            "description": "admin announcement",
            "type": "information",
            "active_from": "2025-01-01T00:00:00Z",
            "active_to": "2026-01-02T00:00:00Z",
        }
        response = self.create_admin_announcement(self.fixture.owner, payload)
        self.assertEqual(response.status_code, 403)

    def create_admin_announcement(self, user, payload):
        self.client.force_authenticate(user)
        response = self.client.post(self.url, payload)
        return response


class AdminAnnouncementGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.admin_announcement = factories.AdminAnnouncementFactory()
        self.url = factories.AdminAnnouncementFactory.get_url(self.admin_announcement)

    def test_anonymous_user_can_get_admin_announcement(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["uuid"], self.admin_announcement.uuid.hex)
        self.assertEqual(
            response.data["description"], self.admin_announcement.description
        )
        self.assertEqual(response.data["type"], self.admin_announcement.type)


@freeze_time("2025-01-01")
class AdminAnnouncementFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.admin_announcement = factories.AdminAnnouncementFactory(
            active_from="2024-01-01T00:00:00Z", active_to="2026-01-02T00:00:00Z"
        )
        self.url = factories.AdminAnnouncementFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def assert_positive(self, response, expected_announcement):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["uuid"], expected_announcement.uuid.hex)
        self.assertEqual(
            response.data[0]["description"], expected_announcement.description
        )
        self.assertEqual(response.data[0]["type"], expected_announcement.type)

    def assert_negative(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_filter_announcements_by_status(self):
        response = self.client.get(self.url + "?is_active=true")
        self.assert_positive(response, self.admin_announcement)

        response = self.client.get(self.url + "?is_active=false")
        self.assert_negative(response)

    def test_filter_announcements_by_type(self):
        response = self.client.get(self.url + "?type=information")
        self.assert_positive(response, self.admin_announcement)

        response = self.client.get(self.url + "?type=warning")
        self.assert_negative(response)
