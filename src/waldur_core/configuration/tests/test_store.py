from unittest import mock

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.db import DatabaseError
from django.test import TestCase, override_settings
from rest_framework import status, test

from waldur_core.configuration import config, registry, secrets
from waldur_core.configuration import models as configuration_models
from waldur_core.configuration.models import Setting, SettingLocked, Source
from waldur_core.configuration.signals import setting_updated
from waldur_core.configuration.testing import override_config
from waldur_core.core import encryption
from waldur_core.structure.tests.factories import UserFactory


class ReadTest(TestCase):
    def test_reading_an_unset_setting_stores_nothing(self):
        self.assertEqual(config.SITE_NAME, registry.default("SITE_NAME"))
        Setting.objects.values_with_defaults()

        self.assertFalse(Setting.objects.exists())

    def test_unknown_setting_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            config.NO_SUCH_SETTING
        self.assertEqual(getattr(config, "NO_SUCH_SETTING", "fallback"), "fallback")

    def test_null_value_reads_as_default(self):
        Setting.objects.create(key="SITE_NAME", value=None, source=Source.API)

        self.assertEqual(config.SITE_NAME, registry.default("SITE_NAME"))
        self.assertEqual(
            Setting.objects.values_with_defaults()["SITE_NAME"],
            registry.default("SITE_NAME"),
        )

    def _unreadable(self, table_read_before):
        return (
            mock.patch.object(configuration_models, "_table_exists", table_read_before),
            mock.patch.object(
                Setting.objects, "filter", side_effect=DatabaseError("no table")
            ),
        )

    def test_table_never_read_reads_as_defaults(self):
        known, failing = self._unreadable(table_read_before=False)
        with known, failing:
            self.assertEqual(config.SITE_NAME, registry.default("SITE_NAME"))

    def test_secret_never_reads_as_empty_while_table_is_unreadable(self):
        # An empty secret switches some checks off, webhook signatures among them.
        known, failing = self._unreadable(table_read_before=False)
        with known, failing:
            value = config.MATRIX_APPSERVICE_HS_TOKEN
        self.assertTrue(value)
        self.assertTrue(secrets.is_undecryptable_value(value))

    def test_error_after_the_table_was_read_is_raised(self):
        known, failing = self._unreadable(table_read_before=True)
        with known, failing, self.assertRaises(DatabaseError):
            config.SITE_NAME

    def test_default_is_a_copy(self):
        config.DISABLED_OFFERING_TYPES.append("Changed.InPlace")

        self.assertEqual(
            config.DISABLED_OFFERING_TYPES, registry.default("DISABLED_OFFERING_TYPES")
        )
        self.assertNotIn("Changed.InPlace", registry.default("DISABLED_OFFERING_TYPES"))


class WriteTest(TestCase):
    def test_write_records_where_it_came_from(self):
        user = UserFactory()

        Setting.objects.set(
            "SITE_NAME",
            "Portal",
            source=Source.DEPLOYMENT,
            source_detail="whitelabeling.yaml",
            user=user,
        )

        row = Setting.objects.get(key="SITE_NAME")
        self.assertEqual(row.value, "Portal")
        self.assertEqual(row.source, Source.DEPLOYMENT)
        self.assertEqual(row.source_detail, "whitelabeling.yaml")
        self.assertEqual(row.updated_by, user)
        self.assertEqual(config.SITE_NAME, "Portal")

    def test_attribute_write_is_attributed_to_waldur(self):
        config.SITE_NAME = "Portal"

        self.assertEqual(Setting.objects.get(key="SITE_NAME").source, "application")

    def test_unknown_setting_cannot_be_written(self):
        with self.assertRaises(AttributeError):
            Setting.objects.set("NO_SUCH_SETTING", 1, source=Source.API)

    def test_set_is_stored_as_list(self):
        Setting.objects.set(
            "DISABLED_OFFERING_TYPES", {"OpenStack.Tenant"}, source=Source.API
        )

        self.assertEqual(config.DISABLED_OFFERING_TYPES, ["OpenStack.Tenant"])

    def test_write_sends_signal_with_stored_values(self):
        received = []

        def receiver(sender, key, old_value, new_value, **kwargs):
            received.append((key, old_value, new_value))

        setting_updated.connect(receiver)
        self.addCleanup(setting_updated.disconnect, receiver)

        config.SITE_NAME = "One"
        config.SITE_NAME = "Two"
        config.MATRIX_APPSERVICE_AS_TOKEN = "as-secret"

        self.assertEqual(received[0], ("SITE_NAME", None, "One"))
        self.assertEqual(received[1], ("SITE_NAME", "One", "Two"))
        key, _, new_value = received[2]
        self.assertTrue(encryption.is_encrypted(new_value))

    def test_write_clears_cached_public_settings(self):
        cache.set("API_CONFIGURATION", {"stale": True})

        config.SITE_NAME = "Portal"

        self.assertIsNone(cache.get("API_CONFIGURATION"))


class SettingsApiProvenanceTest(test.APITestCase):
    def test_api_write_records_the_user(self):
        staff = UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        response = self.client.post(
            "/api/override-settings/", {"SITE_NAME": "Portal"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = Setting.objects.get(key="SITE_NAME")
        self.assertEqual(row.source, Source.API)
        self.assertEqual(row.updated_by, staff)


def lock(key, value, source_detail="whitelabeling.yaml"):
    Setting.objects.set(
        key,
        value,
        source=Source.DEPLOYMENT,
        source_detail=source_detail,
        locked=True,
        managed_by=source_detail,
    )


class LockTest(TestCase):
    def test_people_cannot_change_a_locked_setting(self):
        lock("SITE_NAME", "Portal")

        for source in (Source.API, Source.ADMIN):
            with self.assertRaises(SettingLocked) as cm:
                Setting.objects.set("SITE_NAME", "Other", source=source)
            self.assertIn("whitelabeling.yaml", str(cm.exception.detail["SITE_NAME"]))
        row = Setting.objects.get(key="SITE_NAME")
        self.assertEqual((row.value, row.source), ("Portal", Source.DEPLOYMENT))

    def test_writing_the_locked_value_again_is_a_no_op(self):
        lock("MATRIX_APPSERVICE_AS_TOKEN", "as-secret")

        Setting.objects.set(
            "MATRIX_APPSERVICE_AS_TOKEN", "as-secret", source=Source.API
        )

        row = Setting.objects.get(key="MATRIX_APPSERVICE_AS_TOKEN")
        self.assertEqual(row.source, Source.DEPLOYMENT)

    def test_the_deployment_can_change_a_locked_setting(self):
        lock("SITE_NAME", "Portal")

        Setting.objects.set("SITE_NAME", "Redeployed", source=Source.DEPLOYMENT)

        row = Setting.objects.get(key="SITE_NAME")
        self.assertEqual(row.value, "Redeployed")
        self.assertTrue(row.locked)

    def test_a_write_by_waldur_keeps_what_manages_the_setting(self):
        # The file still re-applies it, and is what releases it again.
        lock("SITE_NAME", "Portal")

        config.SITE_NAME = "By Waldur"

        row = Setting.objects.get(key="SITE_NAME")
        self.assertEqual((row.source, row.locked), (Source.APPLICATION, True))
        self.assertEqual(row.managed_by, "whitelabeling.yaml")

    def test_another_manager_does_not_inherit_the_lock(self):
        lock("MATRIX_LIVEKIT_KEY", "key", source_detail="stdin")

        Setting.objects.set(
            "MATRIX_LIVEKIT_KEY",
            "key",
            source=Source.DEPLOYMENT,
            source_detail="init_matrix_settings",
            managed_by="init_matrix_settings",
        )

        row = Setting.objects.get(key="MATRIX_LIVEKIT_KEY")
        self.assertEqual((row.locked, row.managed_by), (False, "init_matrix_settings"))

    def test_unlock_keeps_the_value_and_the_manager(self):
        lock("SITE_NAME", "Portal")

        Setting.objects.unlock("SITE_NAME")
        Setting.objects.set("SITE_NAME", "Other", source=Source.API)

        row = Setting.objects.get(key="SITE_NAME")
        self.assertEqual((row.value, row.locked), ("Other", False))
        # The next deploy still puts its value back.
        self.assertEqual(row.managed_by, "whitelabeling.yaml")

    def test_release_hands_the_setting_back(self):
        lock("SITE_NAME", "Portal")

        Setting.objects.release("SITE_NAME")

        row = Setting.objects.get(key="SITE_NAME")
        self.assertEqual((row.value, row.locked, row.managed_by), ("Portal", False, ""))


class SettingsApiLockTest(test.APITestCase):
    def setUp(self):
        self.client.force_authenticate(UserFactory(is_staff=True))

    def test_a_change_to_a_locked_setting_is_rejected(self):
        lock("SITE_NAME", "Portal")

        response = self.client.post(
            "/api/override-settings/",
            {"SITE_NAME": "Other", "BRAND_COLOR": "#FF0000"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("whitelabeling.yaml", response.data["SITE_NAME"][0])
        self.assertEqual(config.SITE_NAME, "Portal")
        # Nothing of the request is written.
        self.assertFalse(Setting.objects.filter(key="BRAND_COLOR").exists())

    def test_resubmitting_a_locked_value_is_accepted(self):
        lock("SITE_NAME", "Portal")

        response = self.client.post(
            "/api/override-settings/",
            {"SITE_NAME": "Portal", "BRAND_COLOR": "#FF0000"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(config.BRAND_COLOR, "#FF0000")


class SettingsSourcesApiTest(test.APITestCase):
    url = "/api/override-settings/sources/"

    def test_lists_stored_settings_with_their_source(self):
        staff = UserFactory(is_staff=True)
        lock("SITE_NAME", "Portal")
        Setting.objects.set("BRAND_COLOR", "#FF0000", source=Source.API, user=staff)
        Setting.objects.create(key="SITE_EMAIL", value=None, source=Source.API)
        Setting.objects.create(
            key="POWERED_BY_LOGO",
            value=None,
            source=Source.DEPLOYMENT,
            source_detail="whitelabeling.yaml",
            locked=True,
        )
        self.client.force_authenticate(staff)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = {row["key"]: row for row in response.data}
        self.assertEqual(set(rows), {"BRAND_COLOR", "POWERED_BY_LOGO", "SITE_NAME"})
        self.assertEqual(rows["SITE_NAME"]["source"], Source.DEPLOYMENT)
        self.assertEqual(rows["SITE_NAME"]["source_detail"], "whitelabeling.yaml")
        self.assertTrue(rows["SITE_NAME"]["locked"])
        self.assertEqual(rows["SITE_NAME"]["managed_by"], "whitelabeling.yaml")
        self.assertEqual(rows["BRAND_COLOR"]["managed_by"], "")
        self.assertIsNone(rows["SITE_NAME"]["updated_by_username"])
        self.assertEqual(rows["BRAND_COLOR"]["updated_by_username"], staff.username)
        self.assertFalse(rows["BRAND_COLOR"]["locked"])

    def test_support_can_read_and_users_cannot(self):
        self.client.force_authenticate(UserFactory(is_support=True))
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

        self.client.force_authenticate(UserFactory())
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN
        )


@override_settings(CONFIGURATION_IN_MEMORY=True)
class InMemoryTest(TestCase):
    def test_nothing_is_written_to_the_database(self):
        config.SITE_NAME = "Portal"

        self.assertEqual(config.SITE_NAME, "Portal")
        self.assertEqual(Setting.objects.values_with_defaults()["SITE_NAME"], "Portal")
        self.assertFalse(Setting.objects.exists())

    def tearDown(self):
        configuration_models._memory.clear()


shared_override = override_config(SITE_NAME="Shared")


class OverrideConfigTest(TestCase):
    @override_config(SITE_NAME="Method")
    def test_method_decorator(self):
        self.assertEqual(config.SITE_NAME, "Method")

    def test_context_manager(self):
        with override_config(SITE_NAME="Block"):
            self.assertEqual(config.SITE_NAME, "Block")
        self.assertEqual(config.SITE_NAME, registry.default("SITE_NAME"))

    def test_shared_instance_as_decorator_and_context_manager(self):
        @shared_override
        def read():
            return config.SITE_NAME

        self.assertEqual(read(), "Shared")
        with shared_override:
            self.assertEqual(config.SITE_NAME, "Shared")
        self.assertEqual(config.SITE_NAME, registry.default("SITE_NAME"))

    def test_innermost_override_wins(self):
        with override_config(SITE_NAME="Outer"):
            with override_config(SITE_NAME="Inner"):
                self.assertEqual(config.SITE_NAME, "Inner")
            self.assertEqual(config.SITE_NAME, "Outer")

    def test_override_writes_nothing(self):
        with override_config(SITE_NAME="Block"):
            pass
        self.assertFalse(Setting.objects.exists())

    def test_write_under_override_is_read_back(self):
        with override_config(SITE_NAME="Block"):
            config.SITE_NAME = "Written"
            self.assertEqual(config.SITE_NAME, "Written")
            self.assertEqual(
                Setting.objects.values_with_defaults()["SITE_NAME"], "Written"
            )

    def test_unknown_setting_is_rejected(self):
        with self.assertRaises(AttributeError):
            override_config(NO_SUCH_SETTING=1)

    def test_override_clears_cached_public_settings(self):
        # As a write does: /api/configuration/ is cached until a setting changes.
        cache.set("API_CONFIGURATION", {"stale": True})
        with override_config(SITE_NAME="Block"):
            self.assertIsNone(cache.get("API_CONFIGURATION"))
            cache.set("API_CONFIGURATION", {"stale": True})
        self.assertIsNone(cache.get("API_CONFIGURATION"))


@override_config(SITE_NAME="Class")
class OverrideConfigClassDecoratorTest(TestCase):
    def setUp(self):
        self.site_name_in_set_up = config.SITE_NAME

    def test_override_covers_the_test(self):
        self.assertEqual(config.SITE_NAME, "Class")

    def test_override_covers_set_up(self):
        self.assertEqual(self.site_name_in_set_up, "Class")

    @override_config(SITE_NAME="Method")
    def test_method_override_wins_over_class_override(self):
        self.assertEqual(config.SITE_NAME, "Method")


class OverrideConfigAsyncTest(TestCase):
    @override_config(SITE_NAME="Async")
    async def test_async_test_runs_under_the_override(self):
        self.assertEqual(await sync_to_async(lambda: config.SITE_NAME)(), "Async")
