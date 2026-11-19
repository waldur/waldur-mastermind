from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.models import Setting, SettingLocked, Source
from waldur_core.configuration.tests.test_secrets import LOST_KEY, NEW_KEY
from waldur_core.configuration.tests.utils import logged_setting_changes
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories


class SettingChangeLogTest(TestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)

    def test_change_by_a_person_is_logged_without_the_value(self):
        with logged_setting_changes() as records:
            Setting.objects.set(
                "SITE_EMAIL", "audit@example.com", source=Source.API, user=self.staff
            )

        [record] = records
        self.assertEqual(record.setting_key, "SITE_EMAIL")
        self.assertEqual(record.setting_source, Source.API)
        self.assertEqual(record.user_uuid, self.staff.uuid.hex)
        self.assertEqual(
            record.getMessage(), "Setting SITE_EMAIL has been changed through API."
        )

    def test_change_is_not_an_event(self):
        Event.objects.all().delete()

        Setting.objects.set("SITE_NAME", "Portal", source=Source.API, user=self.staff)

        self.assertFalse(Event.objects.exists())

    def test_change_by_the_deployment_names_its_file(self):
        with logged_setting_changes() as records:
            Setting.objects.set(
                "SITE_NAME",
                "Deployed name",
                source=Source.DEPLOYMENT,
                source_detail="settings.yaml",
            )

        [record] = records
        self.assertEqual(record.setting_source_detail, "settings.yaml")
        self.assertIsNone(record.user_uuid)

    def test_writing_the_same_value_again_is_not_logged(self):
        with logged_setting_changes() as records:
            Setting.objects.set("SITE_NAME", "Portal", source=Source.DEPLOYMENT)
            Setting.objects.set("SITE_NAME", "Portal", source=Source.DEPLOYMENT)

        self.assertEqual(len(records), 1)

    def test_rewriting_a_secret_with_the_same_value_is_not_logged(self):
        with logged_setting_changes() as records:
            Setting.objects.set(
                "AI_ASSISTANT_API_TOKEN", "secret-1", source=Source.ADMIN
            )
            Setting.objects.set(
                "AI_ASSISTANT_API_TOKEN", "secret-1", source=Source.ADMIN
            )

        [record] = records
        self.assertNotIn("secret-1", record.getMessage())

    def test_reordered_multiple_choice_is_not_logged(self):
        key = "DEFAULT_PROPOSAL_HIDDEN_FIELDS"
        Setting.objects.set(key, ["description", "project_summary"], source=Source.API)

        with logged_setting_changes() as records:
            Setting.objects.set(
                key, ["project_summary", "description"], source=Source.API
            )

        self.assertEqual(records, [])

    def test_replacing_an_undecryptable_secret_logs_no_decryption_error(self):
        key = "AI_ASSISTANT_API_TOKEN"
        with override_settings(
            FIELD_ENCRYPTION_KEY=LOST_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            Setting.objects.set(key, "lost", source=Source.ADMIN)

        with override_settings(
            FIELD_ENCRYPTION_KEY=NEW_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            with self.assertNoLogs("waldur_core.configuration.secrets", "ERROR"):
                with logged_setting_changes() as records:
                    Setting.objects.set(key, "fixed", source=Source.ADMIN)

        self.assertEqual(len(records), 1)

    def test_resubmitting_a_locked_multiple_choice_in_another_order_is_accepted(self):
        key = "DEFAULT_PROPOSAL_HIDDEN_FIELDS"
        Setting.objects.set(
            key,
            ["description", "project_summary"],
            source=Source.DEPLOYMENT,
            locked=True,
            managed_by="settings.yaml",
        )

        try:
            Setting.objects.set(
                key, ["project_summary", "description"], source=Source.API
            )
        except SettingLocked:
            self.fail("The same choices in another order were taken as a change.")

    def test_an_unknown_source_is_logged_as_given(self):
        with logged_setting_changes() as records:
            Setting.objects.set("SITE_NAME", "Portal", source="scim")

        [record] = records
        self.assertIn("through scim", record.getMessage())

    def test_change_is_logged_only_once_the_write_is_committed(self):
        # The test settings run on_commit callbacks at once; hand them to the
        # connection instead, as outside tests, and hold them until "commit".
        with mock.patch(
            "waldur_core.core.handlers.transaction.on_commit",
            lambda func, using=None: connection.on_commit(func),
        ):
            with logged_setting_changes() as records:
                with self.captureOnCommitCallbacks() as callbacks:
                    Setting.objects.set("SITE_NAME", "Portal", source=Source.API)

                self.assertEqual(records, [])
                for callback in callbacks:
                    callback()

        self.assertEqual(len(records), 1)

    def test_writes_by_waldur_itself_and_by_migrations_are_not_logged(self):
        with logged_setting_changes() as records:
            Setting.objects.set(
                "SITE_NAME", "By application", source=Source.APPLICATION
            )
            Setting.objects.set("SITE_EMAIL", "m@example.com", source=Source.MIGRATION)

        self.assertEqual(records, [])


class SettingApiChangeLogTest(test.APITestCase):
    def test_change_through_the_api_records_the_user(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        with logged_setting_changes() as records:
            response = self.client.post(
                "/api/override-settings/", {"SITE_NAME": "API portal"}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [record] = records
        self.assertEqual(record.setting_source, Source.API)
        self.assertEqual(record.user_uuid, staff.uuid.hex)


class AdminSettingsPageChangeLogTest(TestCase):
    def test_change_through_the_admin_settings_page_records_the_user(self):
        staff = structure_factories.UserFactory(is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        url = reverse("admin:configuration_setting_changelist")
        page = self.client.get(url).context["form"]
        posted = {}
        for name, field in page.fields.items():
            value = page[name].value()
            if field.disabled or value is None or value is False:
                continue
            if value is True:
                posted[name] = "on"
            elif isinstance(value, str | int):
                posted[name] = value
        posted["SITE_NAME"] = "Admin portal"

        with logged_setting_changes() as records:
            self.client.post(url, posted)

        [record] = [r for r in records if r.setting_key == "SITE_NAME"]
        self.assertEqual(record.setting_source, Source.ADMIN)
        self.assertEqual(record.user_uuid, staff.uuid.hex)
        self.assertIn("through Django admin", record.getMessage())
