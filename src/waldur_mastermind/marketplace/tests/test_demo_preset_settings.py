"""What a demo preset load does to the deployment's settings and features.

A cleanup load returns every setting and feature to its default, except the
settings the deployment owns, then applies what the preset sets. A load
without cleanup leaves settings and features alone.
"""

import json
import pathlib
import tempfile
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, TransactionTestCase

from waldur_core.configuration import config, registry
from waldur_core.configuration.models import Setting, Source
from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.core.models import Feature
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.demo_presets.manifest import DemoPresetManager
from waldur_mastermind.marketplace.management.commands import cleanup_structure

DEPLOYMENT_FILE = "/etc/waldur/override.conf.py"
FEATURE = "project.show_matrix_chat"


def _preset(name, users, settings=None, features=None):
    data = {
        "_metadata": {"title": name, "description": name, "version": "1.0.0"},
        "users": users,
    }
    if settings is not None:
        data["constance_settings"] = settings
    if features is not None:
        data["features"] = [{"key": key, "value": value} for key, value in features]
    return data


def _user(number, username):
    return {
        "uuid": f"{number:032x}",
        "username": username,
        "email": f"{username}@example.com",
        "first_name": username,
        "last_name": "Demo",
        "is_staff": number == 1,
        "is_active": True,
        "password": "demo",
    }


PRESETS = {
    "alpha": _preset(
        "alpha",
        [_user(1, "alpha-staff")],
        settings={"SITE_NAME": "Alpha portal"},
        features=[(FEATURE, True)],
    ),
    "beta": _preset(
        "beta",
        [_user(1, "beta-staff")],
        settings={"CURRENCY_NAME": "USD"},
    ),
    "grabby": _preset(
        "grabby",
        [_user(1, "grabby-staff")],
        settings={
            "HOMEPORT_URL": "https://preset.example.com/",
            "ATLASSIAN_TOKEN": "preset-token",
            "SITE_NAME": "Grabby portal",
        },
    ),
}


# Transaction tests: inside a test case's transaction the cleanup cannot
# TRUNCATE tables with pending foreign key checks, so it would delete nothing.
class PresetSettingsTestMixin:
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, data in PRESETS.items():
            (pathlib.Path(directory.name) / f"{name}.json").write_text(json.dumps(data))
        patcher = mock.patch.object(
            DemoPresetManager, "PRESETS_DIR", pathlib.Path(directory.name)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        cache.clear()
        self.admin = structure_factories.UserFactory(is_staff=True)

    def load(self, name, **kwargs):
        result = DemoPresetManager.load_preset(name, **kwargs)
        self.assertTrue(result["success"], f"{result['message']}\n{result['output']}")
        return result

    def stored(self, key):
        return Setting.objects.filter(key=key).values_list("value", flat=True).first()


class CleanupLoadKeepsDeploymentSettingsTest(
    PresetSettingsTestMixin, TransactionTestCase
):
    def setUp(self):
        super().setUp()
        Setting.objects.set(
            "HOMEPORT_URL",
            "https://portal.example.com/",
            source=Source.DEPLOYMENT,
            source_detail=DEPLOYMENT_FILE,
            locked=True,
            managed_by=DEPLOYMENT_FILE,
            user=self.admin,
        )
        Setting.objects.set(
            "MATRIX_HOMESERVER_URL",
            "https://matrix.example.com",
            source=Source.DEPLOYMENT,
            source_detail=DEPLOYMENT_FILE,
        )
        Setting.objects.set(
            "ATLASSIAN_TOKEN", "deployment-token", source=Source.API, user=self.admin
        )
        Setting.objects.set(
            "TELEMETRY_DEPLOYMENT_ID", "deployment-42", source=Source.CLI
        )
        Setting.objects.set(
            "SITE_DESCRIPTION", "Set by an admin", source=Source.API, user=self.admin
        )
        Feature.objects.create(key="customer.show_project_digest", value=True)

    def test_deployment_owned_settings_survive(self):
        self.load("alpha")

        self.assertEqual(config.HOMEPORT_URL, "https://portal.example.com/")
        self.assertEqual(config.MATRIX_HOMESERVER_URL, "https://matrix.example.com")
        self.assertEqual(config.ATLASSIAN_TOKEN, "deployment-token")
        self.assertEqual(config.TELEMETRY_DEPLOYMENT_ID, "deployment-42")
        row = Setting.objects.get(key="HOMEPORT_URL")
        self.assertTrue(row.locked)
        self.assertEqual(row.managed_by, DEPLOYMENT_FILE)
        self.assertEqual(row.source, Source.DEPLOYMENT)
        self.assertIsNone(row.updated_by)

    def test_other_settings_and_features_return_to_their_defaults(self):
        self.load("alpha")

        self.assertIsNone(self.stored("SITE_DESCRIPTION"))
        self.assertFalse(
            Feature.objects.filter(key="customer.show_project_digest").exists()
        )

    def test_preset_settings_and_features_are_applied(self):
        self.load("alpha")

        self.assertEqual(config.SITE_NAME, "Alpha portal")
        self.assertTrue(Feature.objects.get(key=FEATURE).value)

    def test_loading_a_after_b_is_the_same_as_loading_b(self):
        self.load("alpha")
        self.load("beta")

        self.assertIsNone(self.stored("SITE_NAME"))
        self.assertEqual(config.CURRENCY_NAME, "USD")
        self.assertFalse(Feature.objects.filter(key=FEATURE).exists())

    def test_preset_cannot_override_owned_settings(self):
        result = self.load("grabby")

        self.assertEqual(config.HOMEPORT_URL, "https://portal.example.com/")
        self.assertEqual(config.ATLASSIAN_TOKEN, "deployment-token")
        self.assertEqual(config.SITE_NAME, "Grabby portal")
        self.assertIn("Skipping HOMEPORT_URL", result["output"])
        self.assertIn("Skipping ATLASSIAN_TOKEN", result["output"])


class LoadWithoutCleanupLeavesSettingsAloneTest(
    PresetSettingsTestMixin, TransactionTestCase
):
    def test_settings_and_features_are_untouched(self):
        Setting.objects.set("SITE_DESCRIPTION", "Set by an admin", source=Source.API)
        Feature.objects.create(key="customer.show_project_digest", value=True)

        self.load("beta", cleanup_first=False)

        self.assertEqual(config.SITE_DESCRIPTION, "Set by an admin")
        self.assertTrue(Feature.objects.get(key="customer.show_project_digest").value)
        self.assertEqual(config.CURRENCY_NAME, "USD")


class LoadHasNoSideEffectsTest(PresetSettingsTestMixin, TransactionTestCase):
    def test_generators_run_inside_skip_side_effects(self):
        seen = {}

        def probe(name):
            def run(*args, **kwargs):
                seen[name] = get_skip_side_effects()

            return run

        with (
            mock.patch(
                "waldur_mastermind.marketplace.demo_presets.manifest"
                ".order_history.generate_order_history",
                side_effect=probe("orders"),
            ),
            mock.patch(
                "waldur_mastermind.marketplace.demo_presets.manifest"
                ".proposal_history.generate_proposal_history",
                side_effect=probe("proposals"),
            ),
            mock.patch.object(
                DemoPresetManager,
                "_generate_billing_for_resources",
                side_effect=probe("billing"),
            ),
            mock.patch.object(
                DemoPresetManager,
                "_generate_credit_history",
                side_effect=probe("credits"),
            ),
        ):
            self.load("beta")

        self.assertEqual(
            seen,
            {"orders": True, "proposals": True, "billing": True, "credits": True},
        )
        self.assertFalse(get_skip_side_effects())


class FailedCleanupFailsTheLoadTest(PresetSettingsTestMixin, TransactionTestCase):
    def test_load_stops_when_cleanup_fails(self):
        with mock.patch(
            "waldur_mastermind.marketplace.management.commands.cleanup_structure"
            ".Command._perform_fast_cleanup",
            side_effect=RuntimeError("deadlock detected"),
        ):
            result = DemoPresetManager.load_preset("beta")

        self.assertFalse(result["success"])
        self.assertIn("deadlock detected", result["message"])
        self.assertNotEqual(config.CURRENCY_NAME, "USD")


class ShippedPresetsTest(TestCase):
    def test_no_preset_sets_a_secret_or_runtime_setting(self):
        always_owned = registry.secret_keys() | registry.RUNTIME_SETTINGS
        for path in sorted(DemoPresetManager.PRESETS_DIR.glob("*.json")):
            keys = json.loads(path.read_text()).get("constance_settings") or {}
            with self.subTest(preset=path.stem):
                self.assertFalse(always_owned.intersection(k.upper() for k in keys))


class FailedTableRollsBackTheCleanupTest(PresetSettingsTestMixin, TransactionTestCase):
    def test_nothing_is_deleted_when_one_table_fails(self):
        customer = structure_factories.CustomerFactory()
        Setting.objects.set(
            "HOMEPORT_URL",
            "https://portal.example.com/",
            source=Source.DEPLOYMENT,
            locked=True,
            user=self.admin,
        )
        original = cleanup_structure.Command._fast_delete_table

        def fail_on_users(command, cursor, stat_key, table_name, **kwargs):
            if table_name == "core_user":
                command.stats[stat_key]["errors"] += 1
                return
            return original(command, cursor, stat_key, table_name, **kwargs)

        with mock.patch.object(
            cleanup_structure.Command, "_fast_delete_table", fail_on_users
        ):
            result = DemoPresetManager.load_preset("beta")

        self.assertFalse(result["success"])
        self.assertIn("users", result["message"])
        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())
        self.assertEqual(config.HOMEPORT_URL, "https://portal.example.com/")
        self.assertNotEqual(config.CURRENCY_NAME, "USD")
