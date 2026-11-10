"""What a deployment adds to demo presets: presets of its own, an overlay
imported after every cleanup load, and the password its staff users get."""

import datetime
import json
import pathlib
import tempfile
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.core.models import Feature, User
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.demo_presets import jobs
from waldur_mastermind.marketplace.demo_presets.manifest import DemoPresetManager

FEATURE = "project.show_matrix_chat"
AGENT_TOKEN = "a" * 40
ONE_HOUR = datetime.timedelta(hours=1)


def _user(uuid, username, is_staff=False, **fields):
    return {
        "uuid": uuid,
        "username": username,
        "email": f"{username}@example.com",
        "first_name": username,
        "last_name": "Demo",
        "is_staff": is_staff,
        "is_active": True,
        "password": "demo",
        **fields,
    }


def _customer(uuid, name):
    return {"uuid": uuid, "name": name, "abbreviation": name[:8]}


def _preset(name, users=(), customers=(), **extra):
    return {
        "_metadata": {"title": name.title(), "description": name, "version": "1.0.0"},
        "users": list(users),
        "customers": list(customers),
        **extra,
    }


BUILT_IN = {
    "demo": _preset(
        "demo",
        users=[
            _user("00000000000000000000000000000001", "staff", is_staff=True),
            _user("00000000000000000000000000000002", "owner"),
        ],
        customers=[_customer("a3000000000000000000000000000001", "Built-in Org")],
    ),
    "replaced": _preset("replaced"),
}

DEPLOYMENT = {
    "replaced": _preset(
        "replaced",
        customers=[_customer("a3000000000000000000000000000002", "Deployment Org")],
    ),
    "site": _preset(
        "site",
        users=[_user("00000000000000000000000000000003", "site-owner")],
        customers=[_customer("a3000000000000000000000000000003", "Site Org")],
    ),
    "infra": _preset(
        "infra",
        users=[
            {
                "uuid": "00000000000000000000000000000009",
                "username": "agent",
                "email": "agent@example.com",
                "is_active": True,
                "token_lifetime": None,
            },
            _user("00000000000000000000000000000008", "infra-staff", is_staff=True),
        ],
        customers=[_customer("a3000000000000000000000000000009", "Infra Org")],
        auth_tokens=[
            {
                "key": AGENT_TOKEN,
                "user_uuid": "00000000000000000000000000000009",
                "created": "2026-01-01T00:00:00Z",
            }
        ],
        features=[{"key": FEATURE, "value": True}],
    ),
}


def deployment_settings(**values):
    return override_settings(WALDUR_CORE={**settings.WALDUR_CORE, **values})


class DeploymentPresetsMixin:
    """Built-in presets in one temporary directory and the deployment's in
    another, set as its extra directory."""

    def setUp(self):
        super().setUp()
        self.built_in = self._directory(BUILT_IN)
        self.extra = self._directory(DEPLOYMENT)
        patcher = mock.patch.object(DemoPresetManager, "PRESETS_DIR", self.built_in)
        patcher.start()
        self.addCleanup(patcher.stop)
        overrides = deployment_settings(
            DEMO_PRESETS_ENABLED=True,
            DEMO_PRESETS_ALLOWED=[],
            DEMO_PRESETS_EXTRA_DIRS=[str(self.extra)],
            **self.deployment,
        )
        overrides.enable()
        self.addCleanup(overrides.disable)
        cache.clear()

    deployment = {}

    def _directory(self, presets):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = pathlib.Path(directory.name)
        for name, data in presets.items():
            (path / f"{name}.json").write_text(json.dumps(data))
        return path

    def load(self, name, **kwargs):
        result = DemoPresetManager.load_preset(name, **kwargs)
        self.assertTrue(result["success"], f"{result['message']}\n{result['output']}")
        return result


class ExtraDirectoriesTest(DeploymentPresetsMixin, TestCase):
    def test_deployment_presets_are_listed_with_the_built_in_ones(self):
        names = [preset.name for preset in DemoPresetManager.list_presets()]
        self.assertEqual(names, ["demo", "infra", "replaced", "site"])

    def test_deployment_preset_replaces_a_built_in_one_of_the_same_name(self):
        self.assertEqual(
            DemoPresetManager.get_preset_path("replaced"), self.extra / "replaced.json"
        )
        self.assertEqual(
            DemoPresetManager.get_preset_info("replaced").entity_counts["customers"], 1
        )

    def test_missing_directory_is_skipped(self):
        with deployment_settings(
            DEMO_PRESETS_EXTRA_DIRS=["/nonexistent/presets", str(self.extra)]
        ):
            names = [preset.name for preset in DemoPresetManager.list_presets()]
        self.assertIn("site", names)

    def test_name_cannot_reach_outside_the_directories(self):
        (self.built_in.parent / "outside.json").write_text(json.dumps(_preset("x")))
        self.addCleanup((self.built_in.parent / "outside.json").unlink)
        for name in ("../outside", "/etc/passwd", ".hidden", ""):
            with self.subTest(name=name):
                self.assertIsNone(DemoPresetManager.get_preset_path(name))

    def test_command_loads_a_deployment_preset(self):
        call_command(
            "demo_presets", "load", "site", "-y", "--no-cleanup", stdout=StringIO()
        )
        self.assertTrue(Customer.objects.filter(name="Site Org").exists())
        self.assertTrue(User.objects.filter(username="site-owner").exists())


class ExtraDirectoriesApiTest(DeploymentPresetsMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def test_deployment_preset_is_listed_and_described(self):
        response = self.client.get("/api/marketplace-demo-presets/list/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("site", [preset["name"] for preset in response.data])

        response = self.client.get("/api/marketplace-demo-presets/info/site/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["entity_counts"]["customers"], 1)

    @mock.patch.object(jobs, "purge_task_queues", return_value=0)
    @mock.patch(
        "waldur_mastermind.marketplace.demo_presets.tasks.load_demo_preset.delay"
    )
    def test_deployment_preset_load_is_queued(self, delay, purge):
        response = self.client.post("/api/marketplace-demo-presets/load/site/", {})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        job = models.DemoPresetLoad.objects.get()
        self.assertEqual(job.preset, "site")
        delay.assert_called_once_with(job.uuid.hex)


class OverlayIsNotOfferedTest(DeploymentPresetsMixin, test.APITestCase):
    deployment = {"DEMO_PRESETS_OVERLAY": "infra"}

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def test_overlay_is_not_listed(self):
        response = self.client.get("/api/marketplace-demo-presets/list/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [preset["name"] for preset in response.data]
        self.assertIn("site", names)
        self.assertNotIn("infra", names)

    def test_overlay_cannot_be_described_or_loaded(self):
        response = self.client.get("/api/marketplace-demo-presets/info/infra/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.post("/api/marketplace-demo-presets/load/infra/", {})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(models.DemoPresetLoad.objects.exists())


# Transaction tests: inside a test case's transaction the cleanup cannot
# TRUNCATE tables with pending foreign key checks, so it would delete nothing.
class OverlayTest(DeploymentPresetsMixin, TransactionTestCase):
    deployment = {"DEMO_PRESETS_OVERLAY": "infra"}

    def test_cleanup_load_imports_the_overlay(self):
        result = self.load("demo")

        self.assertIn("overlay infra", result["output"])
        self.assertTrue(Customer.objects.filter(name="Built-in Org").exists())
        self.assertTrue(Customer.objects.filter(name="Infra Org").exists())
        agent = User.objects.get(username="agent")
        self.assertIsNone(agent.token_lifetime)
        self.assertEqual(Token.objects.get(user=agent).key, AGENT_TOKEN)
        self.assertTrue(Feature.objects.get(key=FEATURE).value)

    def test_load_without_cleanup_imports_no_overlay(self):
        self.load("site", cleanup_first=False)

        self.assertTrue(Customer.objects.filter(name="Site Org").exists())
        self.assertFalse(Customer.objects.filter(name="Infra Org").exists())
        self.assertFalse(User.objects.filter(username="agent").exists())

    def test_missing_overlay_fails_the_load_before_anything_is_deleted(self):
        existing = structure_factories.CustomerFactory()
        with deployment_settings(DEMO_PRESETS_OVERLAY="gone"):
            result = DemoPresetManager.load_preset("demo")

        self.assertFalse(result["success"])
        self.assertIn("Overlay preset 'gone' not found", result["message"])
        self.assertTrue(Customer.objects.filter(pk=existing.pk).exists())


class StaffPasswordTest(DeploymentPresetsMixin, TransactionTestCase):
    deployment = {
        "DEMO_PRESETS_OVERLAY": "infra",
        "DEMO_PRESETS_STAFF_PASSWORD": "deployment-secret",
    }

    def test_staff_users_get_the_deployment_password(self):
        result = self.load("demo")

        for username in ("staff", "infra-staff"):
            with self.subTest(username=username):
                user = User.objects.get(username=username)
                self.assertTrue(user.check_password("deployment-secret"))
                self.assertFalse(user.check_password("demo"))
        self.assertTrue(User.objects.get(username="owner").check_password("demo"))
        self.assertNotIn("deployment-secret", result["output"])
        self.assertIn("staff: (the deployment's staff password)", result["output"])

    def test_staff_passwords_are_not_shown(self):
        users = {
            user["username"]: user
            for user in DemoPresetManager.get_preset_users("demo")
        }
        self.assertEqual(users["staff"]["password"], "")
        self.assertEqual(users["owner"]["password"], "demo")

    def test_quick_login_never_lists_staff(self):
        self.load("demo")
        models.DemoPresetLoad.objects.create(
            preset="demo", state=models.DemoPresetLoad.States.DONE
        )

        response = test.APIClient().get("/api/demo-preset-status/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [user["username"] for user in response.data["users"]], ["owner"]
        )


class StaffPasswordIsNotPublishedTest(DeploymentPresetsMixin, test.APITestCase):
    deployment = {"DEMO_PRESETS_STAFF_PASSWORD": "deployment-secret"}

    def test_configuration_does_not_carry_it(self):
        response = self.client.get("/api/configuration/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("deployment-secret", response.content.decode())
        self.assertNotIn("DEMO_PRESETS_STAFF_PASSWORD", response.data["WALDUR_CORE"])


class StaffPasswordUnsetTest(DeploymentPresetsMixin, TestCase):
    def test_preset_password_is_kept(self):
        self.load("demo", cleanup_first=False)
        self.assertTrue(User.objects.get(username="staff").check_password("demo"))


class TokenLifetimeTest(DeploymentPresetsMixin, TestCase):
    def test_explicit_null_keeps_a_new_users_token_from_expiring(self):
        with deployment_settings(TOKEN_LIFETIME=ONE_HOUR):
            self.load("infra", cleanup_first=False)
        self.assertIsNone(User.objects.get(username="agent").token_lifetime)

    def test_missing_lifetime_gets_the_default(self):
        with deployment_settings(TOKEN_LIFETIME=ONE_HOUR):
            self.load("site", cleanup_first=False)
        self.assertEqual(User.objects.get(username="site-owner").token_lifetime, 3600)
