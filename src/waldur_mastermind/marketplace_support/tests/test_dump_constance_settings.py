import os
import tempfile
from io import StringIO
from unittest.mock import patch

import yaml
from constance import LazyConfig
from constance import settings as constance_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase

from waldur_core.core import logos


class DumpConstanceSettingsCommandTest(TestCase):
    def setUp(self):
        self.config = LazyConfig()
        # Store original values for cleanup
        self.original_values = {}
        self.test_settings = [
            "SITE_NAME",
            "SITE_DESCRIPTION",
            "AUTO_APPROVE_USER_TOS",
        ]

        for setting in self.test_settings:
            self.original_values[setting] = getattr(self.config, setting)

    def tearDown(self):
        # Restore original values
        for setting, value in self.original_values.items():
            setattr(self.config, setting, value)

    def test_basic_dump_functionality(self):
        """Test basic dump without options"""
        # Set some test values
        setattr(self.config, "SITE_NAME", "Test Site")
        setattr(self.config, "AUTO_APPROVE_USER_TOS", True)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("dump_constance_settings", output_file)

            # Verify file exists and contains data
            self.assertTrue(os.path.exists(output_file))

            with open(output_file) as f:
                settings_data = yaml.safe_load(f)

            self.assertIsInstance(settings_data, dict)
            self.assertIn("SITE_NAME", settings_data)
            self.assertEqual(settings_data["SITE_NAME"], "Test Site")
            self.assertIn("AUTO_APPROVE_USER_TOS", settings_data)
            self.assertEqual(settings_data["AUTO_APPROVE_USER_TOS"], True)

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_include_defaults_option(self):
        """Test --include-defaults option"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            # First dump without defaults
            call_command("dump_constance_settings", output_file)

            with open(output_file) as f:
                settings_without_defaults = yaml.safe_load(f)

            # Then dump with defaults
            call_command("dump_constance_settings", output_file, include_defaults=True)

            with open(output_file) as f:
                settings_with_defaults = yaml.safe_load(f)

            # Should have more settings when including defaults
            self.assertGreater(
                len(settings_with_defaults), len(settings_without_defaults)
            )

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_include_secrets_option(self):
        """Test --include-secrets option"""
        # Set a test token
        setattr(self.config, "ATLASSIAN_TOKEN", "secret_token_123")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            # First dump without secrets (default behavior)
            call_command("dump_constance_settings", output_file)

            with open(output_file) as f:
                settings_without_secrets = yaml.safe_load(f)

            # Should be redacted
            if "ATLASSIAN_TOKEN" in settings_without_secrets:
                self.assertEqual(
                    settings_without_secrets["ATLASSIAN_TOKEN"], "<redacted>"
                )

            # Then dump with secrets
            call_command("dump_constance_settings", output_file, include_secrets=True)

            with open(output_file) as f:
                settings_with_secrets = yaml.safe_load(f)

            # Should contain actual value
            if "ATLASSIAN_TOKEN" in settings_with_secrets:
                self.assertEqual(
                    settings_with_secrets["ATLASSIAN_TOKEN"], "secret_token_123"
                )

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)
            # Clean up
            setattr(self.config, "ATLASSIAN_TOKEN", "")

    @patch(
        "waldur_mastermind.marketplace_support.management.commands.dump_constance_settings.default_storage"
    )
    def test_media_export_functionality(self, mock_storage):
        """Test --export-media option"""
        # Mock file storage behavior
        mock_storage.exists.return_value = True
        mock_storage.open.return_value.__enter__ = lambda x: SimpleUploadedFile(
            "test.png", b"fake_image_data"
        )
        mock_storage.open.return_value.__exit__ = lambda *args: None

        # Set a test logo
        setattr(self.config, "LOGIN_LOGO", "test_logo.png")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = os.path.join(temp_dir, "settings.yaml")
            media_dir = os.path.join(temp_dir, "media")

            try:
                call_command(
                    "dump_constance_settings",
                    output_file,
                    export_media=media_dir,
                    include_defaults=True,
                )

                # Verify YAML file exists
                self.assertTrue(os.path.exists(output_file))

                # Verify media directory was created
                self.assertTrue(os.path.exists(media_dir))

                # Check YAML content
                with open(output_file) as f:
                    settings_data = yaml.safe_load(f)

                if "LOGIN_LOGO" in settings_data:
                    # Should contain full path when media is exported
                    expected_path = os.path.join(media_dir, "test_logo.png")
                    self.assertEqual(settings_data["LOGIN_LOGO"], expected_path)

            finally:
                # Clean up
                setattr(self.config, "LOGIN_LOGO", "")

    def test_empty_media_fields(self):
        """Test handling of empty logo/image fields"""
        # Clear all logo fields
        for logo_field in logos.LOGO_MAP.keys():
            setattr(self.config, logo_field, "")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("dump_constance_settings", output_file, include_defaults=True)

            with open(output_file) as f:
                settings_data = yaml.safe_load(f)

            # Empty logo fields should be present and empty
            for logo_field in logos.LOGO_MAP.keys():
                if logo_field in settings_data:
                    self.assertEqual(settings_data[logo_field], "")

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_yaml_format_validity(self):
        """Test that generated YAML is valid and well-formatted"""
        # Set various types of data
        setattr(self.config, "SITE_NAME", "Test Site")
        setattr(self.config, "AUTO_APPROVE_USER_TOS", True)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("dump_constance_settings", output_file, include_defaults=True)

            # Verify YAML is valid
            with open(output_file) as f:
                settings_data = yaml.safe_load(f)

            self.assertIsInstance(settings_data, dict)

            # Verify data types are preserved
            if "SITE_NAME" in settings_data:
                self.assertIsInstance(settings_data["SITE_NAME"], str)
            if "AUTO_APPROVE_USER_TOS" in settings_data:
                self.assertIsInstance(settings_data["AUTO_APPROVE_USER_TOS"], bool)

            # Verify keys are sorted
            keys = list(settings_data.keys())
            self.assertEqual(keys, sorted(keys))

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_error_handling_invalid_output_path(self):
        """Test error handling for invalid output paths"""
        # Capture stdout to check error message (command outputs to stdout)
        stdout = StringIO()

        # The command handles errors gracefully and doesn't raise exceptions
        # Instead it prints error messages and returns
        call_command(
            "dump_constance_settings", "/invalid/path/settings.yaml", stdout=stdout
        )

        # Check that an error message was printed
        output = stdout.getvalue()
        self.assertIn("Failed to write to file", output)

    def test_complex_data_types(self):
        """Test handling of list and dict data types"""
        # Find settings with complex types
        list_settings = []
        dict_settings = []

        for name, options in constance_settings.CONFIG.items():
            if len(options) >= 3:
                config_type = options[2]
                if config_type == "list_field":
                    list_settings.append(name)
                elif config_type == "dict_field":
                    dict_settings.append(name)

        # Set test values if we have such fields
        if list_settings:
            test_list_field = list_settings[0]
            setattr(self.config, test_list_field, ["item1", "item2", "item3"])

        if dict_settings:
            test_dict_field = dict_settings[0]
            setattr(self.config, test_dict_field, {"key1": "value1", "key2": "value2"})

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("dump_constance_settings", output_file, include_defaults=True)

            with open(output_file) as f:
                settings_data = yaml.safe_load(f)

            # Verify complex types are preserved
            if list_settings and list_settings[0] in settings_data:
                self.assertIsInstance(settings_data[list_settings[0]], list)
                self.assertEqual(
                    settings_data[list_settings[0]], ["item1", "item2", "item3"]
                )

            if dict_settings and dict_settings[0] in settings_data:
                self.assertIsInstance(settings_data[dict_settings[0]], dict)
                self.assertEqual(
                    settings_data[dict_settings[0]],
                    {"key1": "value1", "key2": "value2"},
                )

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

            # Clean up
            if list_settings:
                setattr(self.config, list_settings[0], [])
            if dict_settings:
                setattr(self.config, dict_settings[0], {})

    def test_sensitive_field_detection(self):
        """Test that sensitive fields are properly detected and redacted"""
        sensitive_patterns = ["password", "token", "secret", "key"]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("dump_constance_settings", output_file)

            with open(output_file) as f:
                settings_data = yaml.safe_load(f)

            # Check that any settings containing sensitive patterns are redacted
            for setting_name, value in settings_data.items():
                if any(
                    pattern in setting_name.lower() for pattern in sensitive_patterns
                ):
                    if value and value != "":  # Only check non-empty values
                        self.assertEqual(
                            value,
                            "<redacted>",
                            f"Sensitive setting {setting_name} should be redacted",
                        )

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)
