from django.core.cache import cache
from rest_framework import status, test

from waldur_core.structure.tests.factories import UserFactory

CONTENT = "# About us\n\n- Item\n\n[Link](https://example.com)"


class AboutUsPageSettingsTest(test.APITestCase):
    def setUp(self):
        self.url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        cache.delete("API_CONFIGURATION")

    def get_public_config(self):
        cache.delete("API_CONFIGURATION")
        self.client.logout()
        response = self.client.get("/api/configuration/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["WALDUR_CORE"]

    def test_page_is_disabled_and_empty_by_default(self):
        config = self.get_public_config()
        self.assertFalse(config["ABOUT_US_PAGE_ENABLED"])
        self.assertEqual(config["ABOUT_US_PAGE_CONTENT"], "")

    def test_staff_can_update_page_and_it_is_publicly_visible(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            self.url,
            {"ABOUT_US_PAGE_ENABLED": True, "ABOUT_US_PAGE_CONTENT": CONTENT},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        self.assertEqual(response.data["ABOUT_US_PAGE_CONTENT"], CONTENT)

        config = self.get_public_config()
        self.assertTrue(config["ABOUT_US_PAGE_ENABLED"])
        self.assertEqual(config["ABOUT_US_PAGE_CONTENT"], CONTENT)

    def test_staff_can_clear_page_content(self):
        self.client.force_login(self.staff)
        self.client.post(self.url, {"ABOUT_US_PAGE_CONTENT": CONTENT})

        response = self.client.post(self.url, {"ABOUT_US_PAGE_CONTENT": ""})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.get_public_config()["ABOUT_US_PAGE_CONTENT"], "")

    def test_non_staff_cannot_update_page(self):
        self.client.force_login(UserFactory())
        response = self.client.post(self.url, {"ABOUT_US_PAGE_CONTENT": CONTENT})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
