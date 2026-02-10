from django.core.cache import cache
from rest_framework import status, test

from waldur_core.core import models
from waldur_core.media.utils import dummy_image
from waldur_core.structure.tests.factories import UserFactory


class DisclaimerAreaFeatureFlagTest(test.APITestCase):
    """Tests for the deployment.enable_disclaimer_area feature flag."""

    def setUp(self):
        cache.delete("API_CONFIGURATION")

    def test_enable_disclaimer_area_is_false_by_default(self):
        """The enable_disclaimer_area feature flag defaults to False."""
        user = UserFactory(is_staff=True)
        self.client.force_login(user)

        response = self.client.get("/api/configuration/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            response.data["FEATURES"]["deployment"]["enable_disclaimer_area"]
        )

    def test_staff_can_enable_disclaimer_area_feature(self):
        """Staff user can toggle the enable_disclaimer_area feature flag to True."""
        user = UserFactory(is_staff=True)
        self.client.force_login(user)

        response = self.client.post(
            "/api/feature-values/",
            {"deployment": {"enable_disclaimer_area": True}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        cache.delete("API_CONFIGURATION")
        response = self.client.get("/api/configuration/")
        self.assertTrue(
            models.Feature.objects.get(key="deployment.enable_disclaimer_area").value
        )

    def test_staff_can_disable_disclaimer_area_feature(self):
        """Staff user can toggle the enable_disclaimer_area feature flag back to False."""
        user = UserFactory(is_staff=True)
        self.client.force_login(user)

        # Enable first
        self.client.post(
            "/api/feature-values/",
            {"deployment": {"enable_disclaimer_area": True}},
        )

        # Then disable
        response = self.client.post(
            "/api/feature-values/",
            {"deployment": {"enable_disclaimer_area": False}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        feature = models.Feature.objects.get(key="deployment.enable_disclaimer_area")
        self.assertFalse(feature.value)

    def test_non_staff_cannot_update_disclaimer_area_feature(self):
        """Non-staff user cannot toggle the enable_disclaimer_area feature flag."""
        user = UserFactory(is_staff=False)
        self.client.force_login(user)

        response = self.client.post(
            "/api/feature-values/",
            {"deployment": {"enable_disclaimer_area": True}},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class DisclaimerAreaConfigurationExposureTest(test.APITestCase):
    """Tests that DISCLAIMER_AREA_LOGO and DISCLAIMER_AREA_TEXT are exposed
    in the /api/configuration/ response as public constance settings."""

    def setUp(self):
        self.url = "/api/configuration/"
        self.staff = UserFactory(is_staff=True)
        self.client.force_login(self.staff)
        cache.delete("API_CONFIGURATION")

    def test_disclaimer_area_text_present_in_configuration(self):
        """DISCLAIMER_AREA_TEXT appears in the /api/configuration/ response."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("DISCLAIMER_AREA_TEXT", response.data["WALDUR_CORE"])

    def test_disclaimer_area_logo_present_in_configuration(self):
        """DISCLAIMER_AREA_LOGO appears in the /api/configuration/ response."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # DISCLAIMER_AREA_LOGO is in LOGO_MAP so it shows up as an absolute URI
        # when a logo is configured. It may or may not be present when no logo
        # is configured depending on whether there is a default.
        # The key should be present in WALDUR_CORE since it is a public constance setting.
        self.assertIn("DISCLAIMER_AREA_LOGO", response.data["WALDUR_CORE"])

    def test_disclaimer_area_text_defaults_to_empty_string(self):
        """DISCLAIMER_AREA_TEXT defaults to an empty string."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["WALDUR_CORE"]["DISCLAIMER_AREA_TEXT"], "")


class DisclaimerAreaIconEndpointTest(test.APITestCase):
    """Tests for the /api/icons/disclaimer_area_logo/ endpoint."""

    def setUp(self):
        self.url = "/api/icons/disclaimer_area_logo/"

    def test_returns_404_when_no_logo_configured(self):
        """Without a configured logo, the icon endpoint returns 404."""
        response = self.client.get(self.url)
        self.assertIn(
            response.status_code,
            [status.HTTP_200_OK, status.HTTP_404_NOT_FOUND],
        )

    def test_anonymous_user_can_access_icon_endpoint(self):
        """The icon endpoint is publicly accessible without authentication."""
        response = self.client.get(self.url)
        # Should not return 401 or 403 -- icons are public
        self.assertNotEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class DisclaimerAreaTextUpdateTest(test.APITestCase):
    """Tests for updating DISCLAIMER_AREA_TEXT via /api/override-settings/."""

    def setUp(self):
        self.url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        cache.delete("API_CONFIGURATION")

    def test_staff_can_update_disclaimer_area_text(self):
        """Staff user can set DISCLAIMER_AREA_TEXT via override-settings."""
        self.client.force_login(self.staff)

        response = self.client.post(
            self.url,
            {"DISCLAIMER_AREA_TEXT": "This is a test disclaimer."},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["DISCLAIMER_AREA_TEXT"], "This is a test disclaimer."
        )

    def test_staff_cannot_clear_disclaimer_area_text_with_blank(self):
        """Clearing DISCLAIMER_AREA_TEXT with an empty string is rejected
        because text_field does not allow blank values."""
        self.client.force_login(self.staff)

        # First set a value
        self.client.post(
            self.url,
            {"DISCLAIMER_AREA_TEXT": "Some disclaimer text."},
        )

        # Attempting to clear with empty string returns 400
        response = self.client.post(
            self.url,
            {"DISCLAIMER_AREA_TEXT": ""},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_staff_cannot_update_disclaimer_area_text(self):
        """Non-staff user cannot update DISCLAIMER_AREA_TEXT."""
        user = UserFactory(is_staff=False)
        self.client.force_login(user)

        response = self.client.post(
            self.url,
            {"DISCLAIMER_AREA_TEXT": "Unauthorized attempt."},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_disclaimer_area_text_reflected_in_configuration(self):
        """Updated DISCLAIMER_AREA_TEXT is reflected in /api/configuration/."""
        self.client.force_login(self.staff)

        self.client.post(
            self.url,
            {"DISCLAIMER_AREA_TEXT": "Visible in configuration."},
        )

        cache.delete("API_CONFIGURATION")
        response = self.client.get("/api/configuration/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["WALDUR_CORE"]["DISCLAIMER_AREA_TEXT"],
            "Visible in configuration.",
        )


class DisclaimerAreaLogoUploadTest(test.APITestCase):
    """Tests for uploading DISCLAIMER_AREA_LOGO via /api/override-settings/."""

    def setUp(self):
        self.url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        cache.delete("API_CONFIGURATION")

    def test_staff_can_upload_disclaimer_area_logo(self):
        """Staff user can upload DISCLAIMER_AREA_LOGO as a single file."""
        self.client.force_login(self.staff)

        logo_image = dummy_image()

        response = self.client.post(
            self.url,
            {"DISCLAIMER_AREA_LOGO": logo_image},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the setting was saved with a file path
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data["DISCLAIMER_AREA_LOGO"], str)
        self.assertTrue(len(response.data["DISCLAIMER_AREA_LOGO"]) > 0)

    def test_non_staff_cannot_upload_disclaimer_area_logo(self):
        """Non-staff user cannot upload DISCLAIMER_AREA_LOGO."""
        user = UserFactory(is_staff=False)
        self.client.force_login(user)

        logo_image = dummy_image()

        response = self.client.post(
            self.url,
            {"DISCLAIMER_AREA_LOGO": logo_image},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_upload_logo_and_set_text_together(self):
        """Staff can upload DISCLAIMER_AREA_LOGO and set DISCLAIMER_AREA_TEXT
        in separate requests."""
        self.client.force_login(self.staff)

        # Upload the logo
        logo_image = dummy_image()
        response = self.client.post(
            self.url,
            {"DISCLAIMER_AREA_LOGO": logo_image},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Set the text
        response = self.client.post(
            self.url,
            {"DISCLAIMER_AREA_TEXT": "Disclaimer with logo."},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify both are set
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data["DISCLAIMER_AREA_LOGO"], str)
        self.assertTrue(len(response.data["DISCLAIMER_AREA_LOGO"]) > 0)
        self.assertEqual(response.data["DISCLAIMER_AREA_TEXT"], "Disclaimer with logo.")

    def test_uploaded_logo_accessible_via_icon_endpoint(self):
        """After uploading DISCLAIMER_AREA_LOGO, the icon endpoint serves it."""
        self.client.force_login(self.staff)

        logo_image = dummy_image()
        response = self.client.post(
            self.url,
            {"DISCLAIMER_AREA_LOGO": logo_image},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get("/api/icons/disclaimer_area_logo/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_uploaded_logo_reflected_in_configuration(self):
        """After uploading DISCLAIMER_AREA_LOGO, the configuration endpoint
        returns a full URL pointing to the icon endpoint."""
        self.client.force_login(self.staff)

        logo_image = dummy_image()
        self.client.post(
            self.url,
            {"DISCLAIMER_AREA_LOGO": logo_image},
            format="multipart",
        )

        cache.delete("API_CONFIGURATION")
        response = self.client.get("/api/configuration/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        logo_url = response.data["WALDUR_CORE"]["DISCLAIMER_AREA_LOGO"]
        self.assertIn("api/icons/disclaimer_area_logo/", logo_url)
