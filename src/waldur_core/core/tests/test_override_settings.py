from constance.test.unittest import override_config
from rest_framework import test

from waldur_core.structure.tests.factories import UserFactory


class OverrideSettingsTest(test.APITestCase):
    def setUp(self):
        self.url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        self.client.force_login(self.staff)

    def _override_settings_and_get(self, payload):
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 200)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response

    @override_config(
        WALDUR_SUPPORT_ENABLED=True,
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        NOTIFY_STAFF_ABOUT_APPROVALS=False,
        WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE=False,
        DISABLE_DARK_THEME=True,
    )
    def test_override_settings_bool_field(self):
        """
        Test post request to "api/override-settings/" doesn't affect the fields that are not passed via payload of the request.
        """

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

        self.assertFalse(response.data["WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE"])
        self.assertTrue(response.data["WALDUR_SUPPORT_ENABLED"])
        self.assertTrue(response.data["ANONYMOUS_USER_CAN_VIEW_OFFERINGS"])
        self.assertFalse(response.data["NOTIFY_STAFF_ABOUT_APPROVALS"])
        self.assertTrue(response.data["DISABLE_DARK_THEME"])

        payload = {
            "WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE": True,
            "ANONYMOUS_USER_CAN_VIEW_OFFERINGS": False,
        }
        response = self._override_settings_and_get(payload)

        self.assertTrue(response.data["WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE"])
        self.assertTrue(response.data["WALDUR_SUPPORT_ENABLED"])
        self.assertFalse(response.data["ANONYMOUS_USER_CAN_VIEW_OFFERINGS"])
        self.assertFalse(response.data["NOTIFY_STAFF_ABOUT_APPROVALS"])
        self.assertTrue(response.data["DISABLE_DARK_THEME"])

    def test_override_settings_list_field(self):
        payload = {"FREEIPA_BLACKLISTED_USERNAMES": ["root", "admin", "system"]}
        response = self._override_settings_and_get(payload)
        self.assertEqual(
            response.data["FREEIPA_BLACKLISTED_USERNAMES"], ["root", "admin", "system"]
        )

    def test_override_settings_country_list_field(self):
        payload = {"COUNTRIES": ["US", "GB", "DE"]}
        response = self._override_settings_and_get(payload)
        self.assertEqual(response.data["COUNTRIES"], ["US", "GB", "DE"])

    def test_non_empty_field_can_be_changed(self):
        payload = {"FREEIPA_GROUPNAME_PREFIX": "hpc_"}
        response = self._override_settings_and_get(payload)
        self.assertEqual(response.data["FREEIPA_GROUPNAME_PREFIX"], "hpc_")

    def test_non_empty_field_can_not_be_blanked_out(self):
        """
        An empty FreeIPA prefix makes every entity in the directory look
        Waldur-managed, so the group synchronization would delete all of them.
        """
        for key in ("FREEIPA_GROUPNAME_PREFIX", "FREEIPA_USERNAME_PREFIX"):
            with self.subTest(key=key):
                response = self.client.post(self.url, {key: ""})
                self.assertEqual(response.status_code, 400)
                self.assertIn(key, response.data)

    def test_override_settings_mixed_fields(self):
        payload = {
            "WALDUR_SUPPORT_ENABLED": True,
            "FREEIPA_BLACKLISTED_USERNAMES": ["root", "admin"],
            "COUNTRIES": ["US", "FR"],
            "SITE_NAME": "Custom Site",
            "SUPPORT_PORTAL_URL": "https://support.example.com",
            "CROSSREF_MAILTO": "test@example.com",
        }
        response = self._override_settings_and_get(payload)
        self.assertEqual(response.data["WALDUR_SUPPORT_ENABLED"], True)
        self.assertEqual(
            response.data["FREEIPA_BLACKLISTED_USERNAMES"], ["root", "admin"]
        )
        self.assertEqual(response.data["COUNTRIES"], ["US", "FR"])
        self.assertEqual(response.data["SITE_NAME"], "Custom Site")
