from django.core.cache import cache
from rest_framework import status, test

from waldur_core.structure.tests.factories import UserFactory


class FontFamilyConfigurationTest(test.APITestCase):
    """Tests that FONT_FAMILY is exposed in /api/configuration/ and can be updated."""

    def setUp(self):
        self.configuration_url = "/api/configuration/"
        self.override_url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        self.client.force_login(self.staff)
        cache.delete("API_CONFIGURATION")

    def tearDown(self):
        cache.delete("API_CONFIGURATION")

    def test_font_family_present_in_configuration(self):
        """FONT_FAMILY appears in the /api/configuration/ response under WALDUR_CORE."""
        response = self.client.get(self.configuration_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("FONT_FAMILY", response.data["WALDUR_CORE"])

    def test_font_family_defaults_to_inter(self):
        """FONT_FAMILY defaults to 'Inter'."""
        response = self.client.get(self.configuration_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["WALDUR_CORE"]["FONT_FAMILY"], "Inter")

    def test_staff_can_update_font_family(self):
        """Staff user can set FONT_FAMILY via /api/override-settings/."""
        response = self.client.post(self.override_url, {"FONT_FAMILY": "Maven Pro"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.override_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["FONT_FAMILY"], "Maven Pro")

    def test_non_staff_cannot_update_font_family(self):
        """Non-staff user cannot update FONT_FAMILY."""
        user = UserFactory(is_staff=False)
        self.client.force_login(user)

        response = self.client.post(self.override_url, {"FONT_FAMILY": "Maven Pro"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_updated_font_family_reflected_in_configuration(self):
        """Updated FONT_FAMILY is reflected in /api/configuration/."""
        self.client.post(self.override_url, {"FONT_FAMILY": "Maven Pro"})

        cache.delete("API_CONFIGURATION")
        response = self.client.get(self.configuration_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["WALDUR_CORE"]["FONT_FAMILY"], "Maven Pro")
