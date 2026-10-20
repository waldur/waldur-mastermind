import importlib
from types import SimpleNamespace

from cryptography.fernet import Fernet
from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.test.utils import override_settings

from waldur_core.configuration import config, registry, secrets
from waldur_core.configuration.migration_utils import (
    CONSTANCE_TABLE,
    encode_constance,
    table_exists,
)
from waldur_core.configuration.models import Setting
from waldur_core.configuration.tests.utils import ConstanceTableMixin
from waldur_core.core import encryption

LOST_KEY = Fernet.generate_key().decode()

migration = importlib.import_module(
    "waldur_core.configuration.migrations.0002_import_constance_settings"
)
schema_editor = SimpleNamespace(
    connection=connection,
    execute=lambda sql: connection.cursor().execute(sql),
)


def insert(key, raw):
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {CONSTANCE_TABLE} (key, value) VALUES (%s, %s)", [key, raw]
        )


class ImportConstanceSettingsTest(ConstanceTableMixin, TestCase):
    def _run(self):
        before = Setting.objects.values_with_defaults()
        migration.import_settings(apps, schema_editor)
        return before

    def test_values_read_the_same_after_the_move(self):
        insert("SITE_NAME", encode_constance("Portal"))
        insert("DISABLED_OFFERING_TYPES", encode_constance(["OpenStack.Tenant"]))
        insert("THUMBNAIL_SIZE", encode_constance(registry.default("THUMBNAIL_SIZE")))
        # A row without a value: constance read it as the default.
        insert("SITE_DESCRIPTION", None)
        # A bare file name rather than an envelope.
        insert("SIDEBAR_LOGO", "sidebar_logo.png")

        self._run()

        self.assertEqual(config.SITE_NAME, "Portal")
        self.assertEqual(config.DISABLED_OFFERING_TYPES, ["OpenStack.Tenant"])
        self.assertEqual(config.THUMBNAIL_SIZE, registry.default("THUMBNAIL_SIZE"))
        self.assertEqual(config.SITE_DESCRIPTION, registry.default("SITE_DESCRIPTION"))
        self.assertEqual(config.SIDEBAR_LOGO, "sidebar_logo.png")

    def test_defaults_stored_by_reads_are_not_moved(self):
        insert("THUMBNAIL_SIZE", encode_constance(registry.default("THUMBNAIL_SIZE")))
        insert("SITE_DESCRIPTION", None)
        insert("SITE_NAME", encode_constance("Portal"))

        self._run()

        rows = dict(Setting.objects.values_list("key", "source_detail"))
        self.assertEqual(rows, {"SITE_NAME": "django-constance"})

    def test_settings_that_no_longer_exist_are_dropped(self):
        insert("REMOVED_LONG_AGO", encode_constance(True))

        self._run()

        self.assertFalse(Setting.objects.filter(key="REMOVED_LONG_AGO").exists())

    def test_encrypted_secret_keeps_its_ciphertext(self):
        ciphertext = encryption.encrypt_value("as-secret")
        insert("MATRIX_APPSERVICE_AS_TOKEN", encode_constance(ciphertext))

        self._run()

        self.assertEqual(
            Setting.objects.get(key="MATRIX_APPSERVICE_AS_TOKEN").value, ciphertext
        )
        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "as-secret")

    def test_plaintext_secret_is_encrypted(self):
        insert("MATRIX_APPSERVICE_AS_TOKEN", encode_constance("as-secret"))

        self._run()

        stored = Setting.objects.get(key="MATRIX_APPSERVICE_AS_TOKEN").value
        self.assertTrue(encryption.is_encrypted(stored))
        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "as-secret")

    def test_undecryptable_secret_is_kept(self):
        with override_settings(
            FIELD_ENCRYPTION_KEY=LOST_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
        ):
            ciphertext = encryption.encrypt_value("gone")
        insert("MATRIX_APPSERVICE_AS_TOKEN", encode_constance(ciphertext))

        self._run()

        self.assertEqual(
            Setting.objects.get(key="MATRIX_APPSERVICE_AS_TOKEN").value, ciphertext
        )
        self.assertEqual(
            config.MATRIX_APPSERVICE_AS_TOKEN,
            secrets.undecryptable_value("MATRIX_APPSERVICE_AS_TOKEN"),
        )

    def test_value_set_before_the_move_is_kept(self):
        insert("SITE_NAME", encode_constance("From constance"))
        Setting.objects.create(key="SITE_NAME", value="Newer", source="migration")

        self._run()

        self.assertEqual(config.SITE_NAME, "Newer")

    def test_table_is_kept_for_the_previous_release(self):
        insert("SITE_NAME", encode_constance("Portal"))

        self._run()

        self.assertTrue(table_exists(connection, CONSTANCE_TABLE))


class ImportOnFreshDatabaseTest(TestCase):
    def test_service_access_mode_is_derived(self):
        self.assertFalse(table_exists(connection, CONSTANCE_TABLE))
        Setting.objects.filter(key="SERVICE_ACCESS_MODE").delete()

        migration.import_settings(apps, schema_editor)

        row = Setting.objects.get(key="SERVICE_ACCESS_MODE")
        self.assertIn(row.value, {"calls", "both", "marketplace"})
