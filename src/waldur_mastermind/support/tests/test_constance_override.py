import os
import shutil
import tempfile
from io import StringIO

import yaml
from django.core.management import call_command
from django.test import TestCase

from waldur_core.media.utils import dummy_image


class OverrideConstanceSettingsTest(TestCase):
    def setUp(self):
        """
        Create a temporary directory for the tests
        """
        self.temp_dir = tempfile.mkdtemp()

    def create_settings_file(self, settings_dict=None):
        """
        Create a settings.yaml file in the temporary directory.
        """
        settings_file = os.path.join(self.temp_dir, "settings.yaml")
        with open(settings_file, "w") as f:
            yaml.dump(settings_dict, f)
        return settings_file

    def test_empty_settings_file(self):
        """
        Test that the command prints a warning when the settings file is empty.
        """
        settings_file = self.create_settings_file()
        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn("Constance settings file is empty", output.getvalue())

    def test_empty_image_file(self):
        """
        Test that the command prints an error when the image file is empty.
        """
        temp_image = os.path.join(self.temp_dir, "test_logo.png")
        open(temp_image, "wb").close()
        settings = {"LOGIN_LOGO": temp_image}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn(
            "Failed to save setting LOGIN_LOGO due to error: ", output.getvalue()
        )

    def test_basic_settings_override(self):
        """
        Test that the command overrides the settings correctly.
        """
        settings = {
            "WALDUR_SUPPORT_ENABLED": True,
            "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE": "zammad",
            "ZAMMAD_API_URL": "https://zammad.example.com/api/",
        }
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        for key, value in settings.items():
            self.assertIn(f"{key} has been set to {value}", output.getvalue())

    def test_basic_support_backend_is_a_valid_choice(self):
        """
        Test that the basic support backend passes choice validation.
        """
        settings = {"WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE": "basic"}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn(
            "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE has been set to basic",
            output.getvalue(),
        )

    def test_password_and_token_redaction(self):
        """
        Test that the command redacts passwords and tokens.
        """
        # Set password and token
        settings = {"ZAMMAD_TOKEN": "secret-token", "SOME_PASSWORD": "secret-password"}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)

        self.assertIn("ZAMMAD_TOKEN has been set to <redacted>", output.getvalue())
        self.assertIn("SOME_PASSWORD has been set to <redacted>", output.getvalue())

    def test_logo_upload(self):
        """
        Test that the command uploads a logo file correctly.
        """
        image_content = dummy_image()
        # Create a test logo file
        temp_image = os.path.join(self.temp_dir, "test_logo.png")
        with open(temp_image, "wb") as f:
            f.write(image_content.read())

        settings = {"LOGIN_LOGO": temp_image}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn("LOGIN_LOGO has been set to test_logo.png.", output.getvalue())

    def test_nonexistent_logo_file(self):
        """
        Test that the command returns an error when the logo file does not exist.
        """
        # Set invalid logo file
        settings = {"SIDEBAR_LOGO": "/not/a/valid/path/to/logo.png"}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)

        self.assertIn("SIDEBAR_LOGO file does not exist", output.getvalue())

    def test_dict_field_validation_success(self):
        """
        Test that the dict field is validated correctly.
        """
        # Set valid dict
        settings = {"DOCKER_RUN_OPTIONS": {"mem_limit": "512m", "cpu_count": 2}}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn(
            "DOCKER_RUN_OPTIONS has been set to {'cpu_count': 2, 'mem_limit': '512m'}",
            output.getvalue(),
        )

    def test_dict_field_validation_failure(self):
        """
        Test that the dict field is validated correctly.
        """
        # Set invalid dict
        settings = {"DOCKER_RUN_OPTIONS": "randomstring"}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn(
            "Failed to save setting DOCKER_RUN_OPTIONS due to error: ",
            output.getvalue(),
        )

    def test_url_field_validation(self):
        """
        Test that the url field is validated correctly.
        """
        # Set invalid url
        settings = {"ZAMMAD_API_URL": "prandomstring"}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn(
            "Failed to save setting ZAMMAD_API_URL due to error: ", output.getvalue()
        )

    def test_color_field_validation(self):
        """
        Test that the color field is validated correctly.
        """
        # Set invalid color
        settings = {"BRAND_COLOR": "randomstring"}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn(
            "Failed to save setting BRAND_COLOR due to error: ", output.getvalue()
        )

        # Set valid color
        settings = {"BRAND_COLOR": "#FF0000"}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn("BRAND_COLOR has been set to", output.getvalue())

    def test_list_field_validation(self):
        """
        Test that the list field is validated correctly.
        """
        # Set invalid list
        settings = {"FREEIPA_BLACKLISTED_USERNAMES": "randomstring"}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn(
            "Failed to save setting FREEIPA_BLACKLISTED_USERNAMES due to error: ",
            output.getvalue(),
        )

        # Set valid list
        settings = {"FREEIPA_BLACKLISTED_USERNAMES": ["root", "admin"]}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_constance_settings", settings_file, stdout=output)
        self.assertIn(
            "FREEIPA_BLACKLISTED_USERNAMES has been set to", output.getvalue()
        )

    def tearDown(self):
        """
        Clean up the temporary directory after the tests.
        """
        shutil.rmtree(self.temp_dir)
