"""What `info/<name>` shows before a demo preset is loaded."""

import json
import pathlib
import tempfile
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status, test

from waldur_core.configuration.models import Setting, Source
from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Feature
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.demo_presets.manifest import DemoPresetManager

MATRIX = "project.show_matrix_chat"
DIGEST = "customer.show_project_digest"
DEPLOYMENT_FILE = "/etc/waldur/override.conf.py"
SHIPPED_DIR = DemoPresetManager.PRESETS_DIR


def _user(uuid, username, **fields):
    return {
        "uuid": uuid,
        "username": username,
        "email": f"{username}@example.com",
        "first_name": username.title(),
        "last_name": "Demo",
        "password": "demo",
        **fields,
    }


DEMO = {
    "_metadata": {"title": "Demo", "description": "A demo", "version": "1.0.0"},
    "users": [
        _user("00000000000000000000000000000001", "staff", is_staff=True),
        _user("00000000000000000000000000000002", "support", is_support=True),
        _user("00000000000000000000000000000003", "owner"),
        _user("00000000000000000000000000000004", "former", is_active=False),
    ],
    "customers": [
        {"uuid": "a3000000000000000000000000000001", "name": "Research Org"},
        {"uuid": "a3000000000000000000000000000002", "name": "Provider Org"},
    ],
    "projects": [
        {
            "uuid": "c3000000000000000000000000000001",
            "name": "Climate",
            "customer_uuid": "a3000000000000000000000000000001",
        }
    ],
    "resources": [
        {
            "uuid": "d3000000000000000000000000000002",
            "name": "Storage",
            "project_uuid": "c3000000000000000000000000000001",
        },
        {
            "uuid": "d3000000000000000000000000000001",
            "name": "Cluster",
            "project_uuid": "c3000000000000000000000000000001",
        },
    ],
    "categories": [{"uuid": "e3000000000000000000000000000001", "title": "HPC"}],
    "offerings": [
        {
            "uuid": "f3000000000000000000000000000002",
            "name": "Slurm B",
            "type": "Marketplace.Slurm",
            "category_uuid": "e3000000000000000000000000000001",
        },
        {
            "uuid": "f3000000000000000000000000000001",
            "name": "Slurm A",
            "type": "Marketplace.Slurm",
            "category_uuid": "e3000000000000000000000000000001",
        },
        {
            "uuid": "f3000000000000000000000000000003",
            "name": "Support",
            "type": "Marketplace.Basic",
        },
    ],
    "user_roles": [
        {
            "user_uuid": "00000000000000000000000000000003",
            "role_name": "CUSTOMER.OWNER",
            "scope_type": "structure.customer",
            "scope_uuid": "a3000000000000000000000000000001",
        },
        {
            "user_uuid": "00000000000000000000000000000003",
            "role_name": "PROJECT.MANAGER",
            "scope_type": "structure.project",
            "scope_uuid": "c3000000000000000000000000000001",
            "is_active": False,
        },
    ],
    "constance_settings": {
        "site_name": "Demo portal",
        "HOMEPORT_URL": "https://preset.example.com/",
        "ATLASSIAN_TOKEN": "preset-token",
        "NOT_A_SETTING": 1,
    },
    "features": [
        {"key": MATRIX, "value": True},
        {"key": "project.no_such_feature", "value": True},
    ],
}

OVERLAY = {
    "_metadata": {"title": "Infra"},
    "users": [_user("00000000000000000000000000000009", "agent")],
    "customers": [{"uuid": "a3000000000000000000000000000009", "name": "Infra Org"}],
    "constance_settings": {"SITE_NAME": "Overlay portal"},
    "features": [{"key": DIGEST, "value": True}],
}


REJECTED = {
    "_metadata": {"title": "Rejected"},
    "users": [],
    "constance_settings": {
        "AI_ASSISTANT_ENABLED_ROLES": "everybody",
        "MATRIX_HOMESERVER_URL": "not a url",
        "SITE_NAME": "Fine",
    },
    "features": [{"key": MATRIX, "value": True}],
}


class PreviewTestMixin:
    deployment = {}

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = pathlib.Path(directory.name)
        for name, data in (("demo", DEMO), ("infra", OVERLAY), ("rejected", REJECTED)):
            (path / f"{name}.json").write_text(json.dumps(data))
        (path / "broken.json").write_text('{"users": [')
        patcher = mock.patch.object(DemoPresetManager, "PRESETS_DIR", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        overrides = override_settings(
            WALDUR_CORE={
                **settings.WALDUR_CORE,
                "DEMO_PRESETS_ENABLED": True,
                "DEMO_PRESETS_ALLOWED": [],
                "DEMO_PRESETS_EXTRA_DIRS": [],
                **self.deployment,
            }
        )
        overrides.enable()
        self.addCleanup(overrides.disable)
        cache.clear()
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def preview(self, name="demo"):
        response = self.client.get(f"/api/marketplace-demo-presets/info/{name}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def setting(self, data, key):
        return next((c for c in data["settings_changes"] if c["key"] == key), None)

    def feature(self, data, key):
        return next((c for c in data["feature_changes"] if c["key"] == key), None)


class PreviewContentTest(PreviewTestMixin, test.APITestCase):
    def test_users_with_roles_and_no_staff_or_support_password(self):
        users = {user["username"]: user for user in self.preview().data["users"]}

        self.assertIsNone(users["staff"]["password"])
        self.assertIsNone(users["support"]["password"])
        self.assertEqual(users["owner"]["password"], "demo")
        self.assertTrue(users["owner"]["is_active"])
        self.assertFalse(users["former"]["is_active"])
        self.assertIsNone(users["former"]["password"])
        self.assertEqual(users["owner"]["full_name"], "Owner Demo")
        self.assertEqual(
            users["owner"]["roles"],
            [
                {
                    "role": "CUSTOMER.OWNER",
                    "scope_type": "structure.customer",
                    "scope_name": "Research Org",
                }
            ],
        )

    def test_organizations_projects_and_resources(self):
        self.assertEqual(
            self.preview().data["organizations"],
            [
                {"name": "Provider Org", "projects": []},
                {
                    "name": "Research Org",
                    "projects": [
                        {"name": "Climate", "resources": ["Cluster", "Storage"]}
                    ],
                },
            ],
        )

    def test_offerings_grouped_by_type_and_category(self):
        self.assertEqual(
            self.preview().data["offerings"],
            [
                {"type": "Marketplace.Basic", "category": "", "names": ["Support"]},
                {
                    "type": "Marketplace.Slurm",
                    "category": "HPC",
                    "names": ["Slurm A", "Slurm B"],
                },
            ],
        )

    def test_entity_counts_and_metadata(self):
        data = self.preview().data
        self.assertEqual(data["title"], "Demo")
        self.assertEqual(data["entity_counts"]["resources"], 2)
        self.assertIsNone(data["overlay"])
        self.assertIsNone(data["overlay_contents"])

    def test_preset_that_cannot_be_read_is_not_found(self):
        response = self.client.get("/api/marketplace-demo-presets/info/broken/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_values_the_load_would_reject_keep_their_default(self):
        data = self.preview("rejected").data
        warnings = data["warnings"]
        for key in ("AI_ASSISTANT_ENABLED_ROLES", "MATRIX_HOMESERVER_URL"):
            with self.subTest(key=key):
                self.assertTrue(
                    any(
                        w.startswith(
                            f"The preset sets {key} to a value the load would reject"
                        )
                        for w in warnings
                    ),
                    warnings,
                )
                self.assertIsNone(self.setting(data, key))
        self.assertEqual(self.setting(data, "SITE_NAME")["new_value"], "Fine")
        # The rejected URL does not count as Matrix configuration.
        self.assertTrue(any(w.startswith("Matrix chat") for w in warnings))

    def test_list_stays_light(self):
        response = self.client.get("/api/marketplace-demo-presets/list/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("users", response.data[0])

    def test_every_shipped_preset_previews(self):
        shipped = sorted(path.stem for path in SHIPPED_DIR.glob("*.json"))
        self.assertTrue(shipped)
        with mock.patch.object(DemoPresetManager, "PRESETS_DIR", SHIPPED_DIR):
            for name in shipped:
                with self.subTest(preset=name):
                    data = self.preview(name).data
                    for section in (
                        "users",
                        "organizations",
                        "offerings",
                        "settings_changes",
                        "feature_changes",
                        "warnings",
                        "entity_counts",
                    ):
                        self.assertIn(section, data)
                    self.assertTrue(
                        all(
                            user["password"] is None
                            for user in data["users"]
                            if user["is_staff"] or user["is_support"]
                        )
                    )


class PreviewSettingsTest(PreviewTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        Setting.objects.set(
            "HOMEPORT_URL",
            "https://portal.example.com/",
            source=Source.DEPLOYMENT,
            source_detail=DEPLOYMENT_FILE,
        )
        Setting.objects.set("ATLASSIAN_TOKEN", "deployment-token", source=Source.API)
        Setting.objects.set("SITE_DESCRIPTION", "Set by an admin", source=Source.API)
        Feature.objects.create(key=DIGEST, value=True)

    def test_preset_value_is_shown_against_the_current_one(self):
        change = self.setting(self.preview().data, "SITE_NAME")
        self.assertEqual(change["new_value"], "Demo portal")
        self.assertEqual(change["source"], "preset")
        self.assertTrue(change["changed"])
        self.assertFalse(change["skipped"])

    def test_setting_the_preset_leaves_out_returns_to_its_default(self):
        change = self.setting(self.preview().data, "SITE_DESCRIPTION")
        self.assertEqual(change["current_value"], "Set by an admin")
        self.assertEqual(change["source"], "default")
        self.assertTrue(change["changed"])

    def test_unchanged_default_is_not_listed(self):
        self.assertIsNone(self.setting(self.preview().data, "CURRENCY_NAME"))

    def test_deployment_setting_the_preset_sets_is_skipped(self):
        change = self.setting(self.preview().data, "HOMEPORT_URL")
        self.assertEqual(change["current_value"], "https://portal.example.com/")
        self.assertEqual(change["new_value"], "https://portal.example.com/")
        self.assertEqual(change["source"], "deployment")
        self.assertTrue(change["skipped"])
        self.assertFalse(change["changed"])

    def test_secret_values_are_never_returned(self):
        response = self.preview()
        change = self.setting(response.data, "ATLASSIAN_TOKEN")
        self.assertTrue(change["secret"])
        self.assertTrue(change["skipped"])
        self.assertIsNone(change["current_value"])
        self.assertIsNone(change["new_value"])
        self.assertNotIn("deployment-token", response.content.decode())
        self.assertNotIn("preset-token", response.content.decode())

    def test_features_the_preset_sets_and_those_it_resets(self):
        data = self.preview().data
        self.assertEqual(
            self.feature(data, MATRIX),
            {
                "key": MATRIX,
                "current_value": False,
                "new_value": True,
                "source": "preset",
                "changed": True,
            },
        )
        self.assertEqual(self.feature(data, DIGEST)["source"], "default")
        self.assertFalse(self.feature(data, DIGEST)["new_value"])

    def test_unknown_setting_and_feature_are_warned_about(self):
        warnings = self.preview().data["warnings"]
        self.assertIn("Unknown setting NOT_A_SETTING would be ignored.", warnings)
        self.assertIn(
            "Unknown feature project.no_such_feature would be ignored.", warnings
        )


class PreviewOverlayTest(PreviewTestMixin, test.APITestCase):
    deployment = {"DEMO_PRESETS_OVERLAY": "infra"}

    def test_overlay_contents_without_passwords(self):
        contents = self.preview().data["overlay_contents"]
        self.assertEqual([u["username"] for u in contents["users"]], ["agent"])
        self.assertIsNone(contents["users"][0]["password"])
        self.assertEqual(
            contents["organizations"], [{"name": "Infra Org", "projects": []}]
        )
        self.assertNotIn(
            "Infra Org", [o["name"] for o in self.preview().data["organizations"]]
        )

    def test_overlay_that_cannot_be_read_is_warned_about(self):
        with override_settings(
            WALDUR_CORE={**settings.WALDUR_CORE, "DEMO_PRESETS_OVERLAY": "broken"}
        ):
            data = self.preview().data
        self.assertIsNone(data["overlay"])
        self.assertIsNone(data["overlay_contents"])
        self.assertTrue(any("'broken' cannot be read" in w for w in data["warnings"]))

    def test_overlay_values_win(self):
        data = self.preview().data
        self.assertEqual(data["overlay"], "infra")
        site_name = self.setting(data, "SITE_NAME")
        self.assertEqual(site_name["new_value"], "Overlay portal")
        self.assertEqual(site_name["source"], "overlay")
        self.assertEqual(self.feature(data, DIGEST)["source"], "overlay")
        self.assertTrue(self.feature(data, DIGEST)["new_value"])

    def test_missing_overlay_is_warned_about(self):
        with override_settings(
            WALDUR_CORE={**settings.WALDUR_CORE, "DEMO_PRESETS_OVERLAY": "gone"}
        ):
            data = self.preview().data
        self.assertIsNone(data["overlay"])
        self.assertTrue(any("'gone' is missing" in w for w in data["warnings"]))


class PreviewRequirementsTest(PreviewTestMixin, test.APITestCase):
    def test_matrix_without_its_configuration_is_warned_about(self):
        warnings = self.preview().data["warnings"]
        self.assertIn(
            "Matrix chat is turned on, but after the load MATRIX_ENABLED, "
            "MATRIX_HOMESERVER_URL, MATRIX_APPSERVICE_AS_TOKEN would be unset.",
            warnings,
        )

    def test_matrix_with_its_deployment_configuration_is_not(self):
        for key, value in (
            ("MATRIX_ENABLED", True),
            ("MATRIX_HOMESERVER_URL", "https://matrix.example.com"),
            ("MATRIX_APPSERVICE_AS_TOKEN", "as-token"),
        ):
            Setting.objects.set(
                key, value, source=Source.DEPLOYMENT, source_detail=DEPLOYMENT_FILE
            )
        response = self.preview()
        self.assertFalse(
            any(w.startswith("Matrix chat") for w in response.data["warnings"])
        )
        self.assertNotIn("as-token", response.content.decode())

    @override_config(MATRIX_ENABLED=True, MATRIX_HOMESERVER_URL="https://m.example.com")
    def test_configuration_a_load_resets_does_not_count(self):
        # Set, but not by the deployment: the load returns it to its default.
        warnings = self.preview().data["warnings"]
        self.assertTrue(any(w.startswith("Matrix chat") for w in warnings))
