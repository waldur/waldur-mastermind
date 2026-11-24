from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.notifications.tests import factories


class AdminAnnouncementCreateTest(test.APITestCase):
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

    @freeze_time("2025-01-01")
    def test_support_can_create_admin_announcement(self):
        payload = self.payload.copy()
        payload["active_from"] = "2025-01-01T00:00:00Z"
        response = self.create_admin_announcement(self.fixture.global_support, payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["description"], "admin announcement")
        self.assertEqual(response.data["type"], "information")

    @freeze_time("2025-01-01")
    def test_announcement_is_hidden_from_anonymous_users_by_default(self):
        payload = self.payload.copy()
        payload["active_from"] = "2025-01-01T00:00:00Z"
        response = self.create_admin_announcement(self.fixture.staff, payload)
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.data["visible_to_anonymous_users"])

        payload["visible_to_anonymous_users"] = True
        response = self.create_admin_announcement(self.fixture.staff, payload)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["visible_to_anonymous_users"])

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


@freeze_time("2025-06-01")
class AdminAnnouncementGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.public = factories.AdminAnnouncementFactory(
            visible_to_anonymous_users=True
        )
        self.hidden = factories.AdminAnnouncementFactory()
        self.public_expired = factories.AdminAnnouncementFactory(
            visible_to_anonymous_users=True,
            active_from="2024-01-01T00:00:00Z",
            active_to="2025-01-01T00:00:00Z",
        )
        self.public_upcoming = factories.AdminAnnouncementFactory(
            visible_to_anonymous_users=True,
            active_from="2025-07-01T00:00:00Z",
            active_to="2025-08-01T00:00:00Z",
        )
        self.list_url = factories.AdminAnnouncementFactory.get_list_url()

    def get_listed_uuids(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        return {item["uuid"] for item in response.data}

    def test_anonymous_user_lists_only_active_announcements_visible_to_them(self):
        self.assertEqual(self.get_listed_uuids(), {self.public.uuid.hex})

    def test_anonymous_user_can_get_announcement_visible_to_them(self):
        response = self.client.get(
            factories.AdminAnnouncementFactory.get_url(self.public)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["uuid"], self.public.uuid.hex)
        self.assertEqual(response.data["description"], self.public.description)
        self.assertEqual(response.data["type"], self.public.type)
        self.assertTrue(response.data["visible_to_anonymous_users"])

    def test_anonymous_user_cannot_get_hidden_or_inactive_announcement(self):
        for announcement in (self.hidden, self.public_expired, self.public_upcoming):
            response = self.client.get(
                factories.AdminAnnouncementFactory.get_url(announcement)
            )
            self.assertEqual(response.status_code, 404)

    def test_authenticated_user_lists_all_announcements(self):
        self.client.force_authenticate(self.fixture.user)
        self.assertEqual(
            self.get_listed_uuids(),
            {
                self.public.uuid.hex,
                self.hidden.uuid.hex,
                self.public_expired.uuid.hex,
                self.public_upcoming.uuid.hex,
            },
        )


@freeze_time("2025-01-01")
class AdminAnnouncementFilterTest(test.APITestCase):
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
