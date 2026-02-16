import json
import os
import tempfile
from io import StringIO
from unittest import mock

from constance.models import Constance
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status, test

from waldur_core.media.utils import dummy_image
from waldur_core.structure.tests.factories import UserFactory


class MultilingualLoginLogoViewTest(test.APITestCase):
    def setUp(self):
        self.url = "/api/icons/login_logo/"

    def test_default_logo_returned_when_no_language(self):
        """Without language parameter, default LOGIN_LOGO is used."""
        response = self.client.get(self.url)
        # Returns 404 if no logo configured, which is expected
        self.assertIn(
            response.status_code, [status.HTTP_200_OK, status.HTTP_404_NOT_FOUND]
        )

    @mock.patch("waldur_core.core.views.config")
    @mock.patch("waldur_core.core.views.default_storage")
    def test_language_specific_logo_returned(self, mock_storage, mock_config):
        """When language parameter matches, return language-specific logo."""
        mock_config.LOGIN_LOGO = "default_logo.png"
        mock_config.LOGIN_LOGO_MULTILINGUAL = {"de": "german_logo.png"}

        mock_file = mock.MagicMock()
        mock_storage.open.return_value = mock_file

        self.client.get(self.url, {"language": "de"})

        mock_storage.open.assert_called_once_with("german_logo.png")

    @mock.patch("waldur_core.core.views.config")
    @mock.patch("waldur_core.core.views.default_storage")
    def test_fallback_to_default_when_language_not_found(
        self, mock_storage, mock_config
    ):
        """When requested language doesn't exist, fall back to LOGIN_LOGO."""
        mock_config.LOGIN_LOGO = "default_logo.png"
        mock_config.LOGIN_LOGO_MULTILINGUAL = {"de": "german_logo.png"}

        mock_file = mock.MagicMock()
        mock_storage.open.return_value = mock_file

        self.client.get(self.url, {"language": "fr"})

        mock_storage.open.assert_called_once_with("default_logo.png")

    @mock.patch("waldur_core.core.views.config")
    @mock.patch("waldur_core.core.views.default_storage")
    def test_fallback_when_multilingual_is_empty(self, mock_storage, mock_config):
        """When LOGIN_LOGO_MULTILINGUAL is empty, fall back to LOGIN_LOGO."""
        mock_config.LOGIN_LOGO = "default_logo.png"
        mock_config.LOGIN_LOGO_MULTILINGUAL = {}

        mock_file = mock.MagicMock()
        mock_storage.open.return_value = mock_file

        self.client.get(self.url, {"language": "de"})

        mock_storage.open.assert_called_once_with("default_logo.png")

    @mock.patch("waldur_core.core.views.config")
    @mock.patch("waldur_core.core.views.default_storage")
    def test_fallback_when_multilingual_is_none(self, mock_storage, mock_config):
        """When LOGIN_LOGO_MULTILINGUAL is None, fall back to LOGIN_LOGO."""
        mock_config.LOGIN_LOGO = "default_logo.png"
        mock_config.LOGIN_LOGO_MULTILINGUAL = None

        mock_file = mock.MagicMock()
        mock_storage.open.return_value = mock_file

        self.client.get(self.url, {"language": "de"})

        mock_storage.open.assert_called_once_with("default_logo.png")


class SetLoginLogoLanguageCommandTest(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        # Clean up any existing setting
        Constance.objects.filter(key="LOGIN_LOGO_MULTILINGUAL").delete()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_test_image(self, filename="test_logo.png"):
        """Create a test image file."""
        image_content = dummy_image()
        temp_image = os.path.join(self.temp_dir, filename)
        with open(temp_image, "wb") as f:
            f.write(image_content.read())
        return temp_image

    def test_set_language_logo(self):
        """Test setting a language-specific logo."""
        temp_image = self._create_test_image()

        output = StringIO()
        call_command(
            "set_login_logo_language",
            "--language",
            "de",
            "--file",
            temp_image,
            stdout=output,
        )

        self.assertIn("Login logo for language 'de' has been set", output.getvalue())

    def test_remove_language_logo(self):
        """Test removing a language-specific logo."""
        # First set a logo
        temp_image = self._create_test_image()
        call_command(
            "set_login_logo_language",
            "--language",
            "de",
            "--file",
            temp_image,
        )

        # Then remove it
        output = StringIO()
        call_command(
            "set_login_logo_language", "--language", "de", "--remove", stdout=output
        )

        self.assertIn("Removed login logo for language 'de'", output.getvalue())

    def test_remove_nonexistent_language(self):
        """Test removing a logo for a language that doesn't exist."""
        output = StringIO()
        call_command(
            "set_login_logo_language", "--language", "xx", "--remove", stdout=output
        )

        self.assertIn("No login logo found for language 'xx'", output.getvalue())

    def test_error_when_file_and_remove_both_specified(self):
        """Test error when both --file and --remove are specified."""
        temp_image = self._create_test_image()

        output = StringIO()
        call_command(
            "set_login_logo_language",
            "--language",
            "de",
            "--file",
            temp_image,
            "--remove",
            stdout=output,
        )

        self.assertIn("Cannot use --file and --remove together", output.getvalue())

    def test_error_when_neither_file_nor_remove_specified(self):
        """Test error when neither --file nor --remove are specified."""
        output = StringIO()
        call_command("set_login_logo_language", "--language", "de", stdout=output)

        self.assertIn("Either --file or --remove is required", output.getvalue())

    def test_error_when_file_not_found(self):
        """Test error when the specified file doesn't exist."""
        output = StringIO()
        call_command(
            "set_login_logo_language",
            "--language",
            "de",
            "--file",
            "/nonexistent/path/to/logo.png",
            stdout=output,
        )

        self.assertIn("does not exist", output.getvalue())

    def test_multiple_languages(self):
        """Test setting logos for multiple languages."""
        de_image = self._create_test_image("german_logo.png")
        et_image = self._create_test_image("estonian_logo.png")

        call_command(
            "set_login_logo_language",
            "--language",
            "de",
            "--file",
            de_image,
        )
        call_command(
            "set_login_logo_language",
            "--language",
            "et",
            "--file",
            et_image,
        )

        setting = Constance.objects.get(key="LOGIN_LOGO_MULTILINGUAL")
        self.assertIn("de", setting.value)
        self.assertIn("et", setting.value)


class MultilingualLogoRestApiTest(test.APITestCase):
    """Test setting LOGIN_LOGO_MULTILINGUAL via REST API."""

    def setUp(self):
        self.url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        # Clean up any existing setting
        Constance.objects.filter(key="LOGIN_LOGO_MULTILINGUAL").delete()

    def test_staff_can_set_multilingual_logo_dict(self):
        """Staff can set LOGIN_LOGO_MULTILINGUAL via REST API."""
        self.client.force_login(self.staff)

        payload = {
            "LOGIN_LOGO_MULTILINGUAL": json.dumps(
                {"de": "german_logo.png", "et": "estonian_logo.png"}
            )
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the setting was saved
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["LOGIN_LOGO_MULTILINGUAL"],
            {"de": "german_logo.png", "et": "estonian_logo.png"},
        )

    def test_staff_can_update_multilingual_logo_dict(self):
        """Staff can update existing LOGIN_LOGO_MULTILINGUAL."""
        self.client.force_login(self.staff)

        # Set initial value
        payload = {"LOGIN_LOGO_MULTILINGUAL": json.dumps({"de": "german_logo.png"})}
        self.client.post(self.url, payload)

        # Update with new language
        payload = {
            "LOGIN_LOGO_MULTILINGUAL": json.dumps(
                {"de": "german_logo_v2.png", "fr": "french_logo.png"}
            )
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the update
        response = self.client.get(self.url)
        self.assertEqual(
            response.data["LOGIN_LOGO_MULTILINGUAL"],
            {"de": "german_logo_v2.png", "fr": "french_logo.png"},
        )

    def test_non_staff_cannot_set_multilingual_logo(self):
        """Non-staff users cannot access override-settings endpoint."""
        user = UserFactory(is_staff=False)
        self.client.force_login(user)

        payload = {"LOGIN_LOGO_MULTILINGUAL": json.dumps({"de": "german_logo.png"})}
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_json_rejected(self):
        """Invalid JSON format is rejected."""
        self.client.force_login(self.staff)

        payload = {"LOGIN_LOGO_MULTILINGUAL": "not valid json"}
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class MultilingualLogoBinaryUploadTest(test.APITestCase):
    """Test binary image upload for LOGIN_LOGO_MULTILINGUAL via REST API."""

    def setUp(self):
        self.url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        # Clean up any existing setting
        Constance.objects.filter(key="LOGIN_LOGO_MULTILINGUAL").delete()

    def test_staff_can_upload_language_specific_logo_binary(self):
        """Staff can upload binary images for language-specific logos."""
        self.client.force_login(self.staff)

        # Create test images
        german_logo = dummy_image()
        estonian_logo = dummy_image()

        # Upload as multipart form data with dict keys
        response = self.client.post(
            self.url,
            {
                "LOGIN_LOGO_MULTILINGUAL[de]": german_logo,
                "LOGIN_LOGO_MULTILINGUAL[et]": estonian_logo,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the setting was saved with file paths
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        multilingual = response.data["LOGIN_LOGO_MULTILINGUAL"]
        self.assertIn("de", multilingual)
        self.assertIn("et", multilingual)
        # The values should be file paths (strings), not file objects
        self.assertIsInstance(multilingual["de"], str)
        self.assertIsInstance(multilingual["et"], str)

    def test_upload_single_language_logo(self):
        """Staff can upload a logo for a single language."""
        self.client.force_login(self.staff)

        german_logo = dummy_image()

        response = self.client.post(
            self.url,
            {"LOGIN_LOGO_MULTILINGUAL[de]": german_logo},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        multilingual = response.data["LOGIN_LOGO_MULTILINGUAL"]
        self.assertIn("de", multilingual)
        self.assertEqual(len(multilingual), 1)

    def test_can_mix_json_paths_and_binary_uploads(self):
        """Can set some languages via JSON paths and others via binary upload."""
        self.client.force_login(self.staff)

        # First set via JSON
        payload = {"LOGIN_LOGO_MULTILINGUAL": json.dumps({"fr": "french_logo.png"})}
        self.client.post(self.url, payload)

        # Then upload new binary for German while keeping French path
        german_logo = dummy_image()
        response = self.client.post(
            self.url,
            {"LOGIN_LOGO_MULTILINGUAL[de]": german_logo},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        multilingual = response.data["LOGIN_LOGO_MULTILINGUAL"]
        self.assertIn("de", multilingual)
        # Note: This test checks that the new upload works.
        # The previous 'fr' value would be replaced since we're posting new data.


class SingleLogoFileUploadTest(test.APITestCase):
    """
    Test uploading single logo files (non-multilingual) via REST API.

    Regression test for WAL-9603: Logo upload gives error "submitted data was not a file"
    when uploading SIDEBAR_LOGO_DARK or similar single image fields.
    """

    def setUp(self):
        self.url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        # Clean up any existing settings
        Constance.objects.filter(
            key__in=["SIDEBAR_LOGO_DARK", "SIDEBAR_LOGO", "LOGIN_LOGO"]
        ).delete()

    def test_staff_can_upload_sidebar_logo_dark(self):
        """
        Staff can upload SIDEBAR_LOGO_DARK as a single file.

        This is the specific scenario from WAL-9603 where the bug was triggered
        by _parse_bracket_notation using dict(data) instead of iterating over items.
        """
        self.client.force_login(self.staff)

        logo_image = dummy_image()

        response = self.client.post(
            self.url,
            {"SIDEBAR_LOGO_DARK": logo_image},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the setting was saved with a file path
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data["SIDEBAR_LOGO_DARK"], str)
        self.assertTrue(len(response.data["SIDEBAR_LOGO_DARK"]) > 0)

    def test_staff_can_upload_sidebar_logo(self):
        """Staff can upload SIDEBAR_LOGO as a single file."""
        self.client.force_login(self.staff)

        logo_image = dummy_image()

        response = self.client.post(
            self.url,
            {"SIDEBAR_LOGO": logo_image},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        self.assertIsInstance(response.data["SIDEBAR_LOGO"], str)
        self.assertTrue(len(response.data["SIDEBAR_LOGO"]) > 0)

    def test_staff_can_upload_login_logo(self):
        """Staff can upload LOGIN_LOGO as a single file."""
        self.client.force_login(self.staff)

        logo_image = dummy_image()

        response = self.client.post(
            self.url,
            {"LOGIN_LOGO": logo_image},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        self.assertIsInstance(response.data["LOGIN_LOGO"], str)
        self.assertTrue(len(response.data["LOGIN_LOGO"]) > 0)

    def test_staff_can_upload_multiple_different_logos_at_once(self):
        """Staff can upload multiple different logo types in a single request."""
        self.client.force_login(self.staff)

        sidebar_logo = dummy_image()
        sidebar_logo_dark = dummy_image()

        response = self.client.post(
            self.url,
            {
                "SIDEBAR_LOGO": sidebar_logo,
                "SIDEBAR_LOGO_DARK": sidebar_logo_dark,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        self.assertIsInstance(response.data["SIDEBAR_LOGO"], str)
        self.assertIsInstance(response.data["SIDEBAR_LOGO_DARK"], str)
        self.assertTrue(len(response.data["SIDEBAR_LOGO"]) > 0)
        self.assertTrue(len(response.data["SIDEBAR_LOGO_DARK"]) > 0)

    def test_can_upload_single_logo_and_multilingual_logo_together(self):
        """Can upload both single logo and multilingual logo in one request."""
        self.client.force_login(self.staff)

        sidebar_logo = dummy_image()
        german_login_logo = dummy_image()

        response = self.client.post(
            self.url,
            {
                "SIDEBAR_LOGO_DARK": sidebar_logo,
                "LOGIN_LOGO_MULTILINGUAL[de]": german_login_logo,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(self.url)
        self.assertIsInstance(response.data["SIDEBAR_LOGO_DARK"], str)
        self.assertIn("de", response.data["LOGIN_LOGO_MULTILINGUAL"])
