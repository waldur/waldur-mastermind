import logging
from unittest import mock

from constance import settings as constance_settings
from constance.models import Constance
from constance.utils import get_values
from django.test import TestCase

from waldur_core.core import views


class TestPublicSettings(TestCase):
    def setUp(self):
        super().setUp()

        class MockExtension:
            def __init__(self, name):
                class Settings:
                    def __init__(self, name):
                        setattr(self, name, {})

                self.Settings = Settings(name)

            @staticmethod
            def get_public_settings():
                return ["INFO"]

            @staticmethod
            def get_dynamic_settings():
                return {"DYN": "dynamic"}

        extensions = {
            "WALDUR_CORE": {},
            "WALDUR_AUTH_SOCIAL": {},
            "WALDUR_FREEIPA": {},
            "WALDUR_HPC": {},
            "WALDUR_SLURM": {},
            "WALDUR_PID": {},
            "WALDUR_AUTH_SAML2": {},
            "WALDUR_MARKETPLACE": {},
            "WALDUR_OPENSTACK": {},
            "WALDUR_EXTENSION_1": {"ENABLED": False},
            "WALDUR_EXTENSION_2": {"ENABLED": True},
            "WALDUR_EXTENSION_3": {"SECRET": "secret", "INFO": "info"},
            "LANGUAGE_CODE": "en",
            "LANGUAGES": (("en", "English"), ("et", "Eesti")),
            "LANGUAGE_CHOICES": ["en"],
            "WALDUR_OPENPORTAL": {"ENABLED": False},
        }
        mock_settings = mock.Mock(**extensions)
        self.patcher_settings = mock.patch(
            "waldur_core.core.views.settings", new=mock_settings
        )
        self.patcher_settings.start()

        self.patcher = mock.patch("waldur_core.core.views.WaldurExtension")
        self.mock = self.patcher.start()
        self.mock.get_extensions.return_value = [
            MockExtension(e) for e in extensions.keys()
        ]

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_if_extension_not_have_field_enabled_or_it_equally_true_this_extension_must_by_in_response(
        self,
    ):
        response = views.get_public_settings()
        self.assertTrue("WALDUR_EXTENSION_2" in response.keys())
        self.assertTrue("WALDUR_EXTENSION_3" in response.keys())

    def test_if_extension_have_field_enabled_and_it_equally_false_this_extension_not_to_be_in_response(
        self,
    ):
        response = views.get_public_settings()
        self.assertFalse("WALDUR_EXTENSION_1" in response.keys())

    def test_if_field_in_get_public_settings_it_value_must_by_in_response(self):
        response = views.get_public_settings()
        self.assertTrue("INFO" in response["WALDUR_EXTENSION_3"])
        self.assertTrue("DYN" in response["WALDUR_EXTENSION_3"])

    def test_if_field_not_in_get_public_settings_it_value_not_to_be_in_response(self):
        response = views.get_public_settings()
        self.assertFalse("SECRET" in response["WALDUR_EXTENSION_3"])


class TestSafeGetConstanceValues(TestCase):
    def test_null_constance_value_returns_none_and_logs_warning(self):
        """When a constance setting has a NULL value in the database,
        _safe_get_constance_values should return None for that key
        and log a warning instead of raising TypeError."""
        # Pick the first constance config key to corrupt
        key = next(iter(constance_settings.CONFIG))
        prefix = constance_settings.DATABASE_PREFIX
        prefixed_key = f"{prefix}{key}"

        # Write a NULL value directly in the database
        Constance.objects.update_or_create(key=prefixed_key, defaults={"value": None})

        with self.assertLogs("waldur_core.core.views", level=logging.WARNING) as cm:
            result = views._safe_get_constance_values()

        self.assertIsNone(result[key])
        self.assertTrue(
            any(key in message for message in cm.output),
            f"Expected warning mentioning '{key}' in logs, got: {cm.output}",
        )

    def test_valid_constance_values_returned_normally(self):
        """When all constance values are valid,
        _safe_get_constance_values returns the same result as get_values."""
        result = views._safe_get_constance_values()
        expected = get_values()
        self.assertEqual(result, expected)


class TestGetConstancePluginSettings(TestCase):
    def test_returns_empty_dict_when_constance_values_empty(self):
        result = views.get_constance_plugin_settings({}, "WALDUR_SUPPORT", ["ENABLED"])
        self.assertEqual(result, {})

    def test_returns_values_for_public_constance_keys(self):
        all_values = {
            "WALDUR_SUPPORT_ENABLED": True,
            "WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE": False,
            "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE": "zammad",
        }
        result = views.get_constance_plugin_settings(
            all_values,
            "WALDUR_SUPPORT",
            ["ENABLED", "DISPLAY_REQUEST_TYPE", "ACTIVE_BACKEND_TYPE"],
        )
        self.assertEqual(result["ENABLED"], True)
        self.assertEqual(result["DISPLAY_REQUEST_TYPE"], False)
        self.assertEqual(result["ACTIVE_BACKEND_TYPE"], "zammad")

    def test_null_value_returned_as_none(self):
        """When a constance value was NULL in DB, _safe_get_constance_values
        stores None. get_constance_plugin_settings should pass it through
        without crashing."""
        all_values = {
            "WALDUR_SUPPORT_ENABLED": None,
        }
        result = views.get_constance_plugin_settings(
            all_values, "WALDUR_SUPPORT", ["ENABLED"]
        )
        self.assertIsNone(result["ENABLED"])

    def test_missing_key_not_included(self):
        """Fields not present in all_constance_values get None from dict.get."""
        result = views.get_constance_plugin_settings(
            {"WALDUR_SUPPORT_ENABLED": True},
            "WALDUR_SUPPORT",
            ["ENABLED", "DISPLAY_REQUEST_TYPE"],
        )
        self.assertEqual(result["ENABLED"], True)
        self.assertIsNone(result["DISPLAY_REQUEST_TYPE"])
