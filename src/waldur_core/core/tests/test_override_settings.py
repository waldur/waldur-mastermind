from constance.test.pytest import override_config
from rest_framework import test

from waldur_core.structure.tests.factories import UserFactory


class OverrideSettingsTest(test.APITransactionTestCase):
    @override_config(
        WALDUR_SUPPORT_ENABLED=True,
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        NOTIFY_STAFF_ABOUT_APPROVALS=False,
        WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE=False,
        DISABLE_DARK_THEME=True,
    )
    def test_override_settings(self):
        """
        Test post request to "api/override-settings/" doesn't affect the fields that are not passed via payload of the request.
        """
        user = UserFactory(is_staff=True)
        self.client.force_login(user)

        response = self.client.get("/api/override-settings/")
        self.assertFalse(response.data["WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE"])
        self.assertTrue(response.data["WALDUR_SUPPORT_ENABLED"])
        self.assertTrue(response.data["ANONYMOUS_USER_CAN_VIEW_OFFERINGS"])
        self.assertFalse(response.data["NOTIFY_STAFF_ABOUT_APPROVALS"])
        self.assertTrue(response.data["DISABLE_DARK_THEME"])

        payload = {
            "WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE": True,
            "ANONYMOUS_USER_CAN_VIEW_OFFERINGS": False,
        }
        response = self.client.post("/api/override-settings/", payload)
        self.assertEqual(response.status_code, 200)

        response = self.client.get("/api/override-settings/")
        self.assertTrue(response.data["WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE"])
        self.assertTrue(response.data["WALDUR_SUPPORT_ENABLED"])
        self.assertFalse(response.data["ANONYMOUS_USER_CAN_VIEW_OFFERINGS"])
        self.assertFalse(response.data["NOTIFY_STAFF_ABOUT_APPROVALS"])
        self.assertTrue(response.data["DISABLE_DARK_THEME"])
