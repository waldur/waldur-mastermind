import importlib
import json
from types import SimpleNamespace

from django.apps import apps as global_apps
from django.db import connection
from django.test import TestCase

from waldur_core.core import encryption
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.tests import factories

_migration = importlib.import_module(
    "waldur_core.structure.migrations.0080_encrypt_servicesettings_credentials"
)
_options_migration = importlib.import_module(
    "waldur_core.structure.migrations.0081_encrypt_servicesettings_options"
)


def _raw_options(settings):
    """The options column as physically stored, bypassing the field."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT options FROM structure_servicesettings WHERE id = %s",
            [settings.id],
        )
        return json.loads(cursor.fetchone()[0])


def _raw(settings):
    """The password/token columns as physically stored, bypassing the field."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT password, token FROM structure_servicesettings WHERE id = %s",
            [settings.id],
        )
        return cursor.fetchone()


class ServiceSettingsEncryptionTest(TestCase):
    def test_password_and_token_encrypted_at_rest(self):
        settings = factories.ServiceSettingsFactory(password="s3kret", token="t0ken")
        password, token = _raw(settings)
        self.assertTrue(password.startswith("gAAAA"))
        self.assertTrue(token.startswith("gAAAA"))

    def test_decrypted_on_read(self):
        settings = factories.ServiceSettingsFactory(password="s3kret", token="t0ken")
        fresh = ServiceSettings.objects.get(pk=settings.pk)
        self.assertEqual(fresh.password, "s3kret")
        self.assertEqual(fresh.token, "t0ken")

    def test_empty_values_are_not_encrypted(self):
        settings = factories.ServiceSettingsFactory(password="", token=None)
        password, token = _raw(settings)
        self.assertIn(password, ("", None))
        self.assertIsNone(token)


class ServiceSettingsTrackerTest(TestCase):
    def test_unchanged_resave_is_not_a_tracked_change(self):
        settings = factories.ServiceSettingsFactory(password="s3kret")
        fresh = ServiceSettings.objects.get(pk=settings.pk)
        # Encrypted column yields fresh ciphertext each write, but the plaintext
        # attribute is unchanged, so the tracker must report no change.
        self.assertFalse(fresh.tracker.has_changed("password"))

    def test_real_change_is_detected(self):
        settings = factories.ServiceSettingsFactory(password="s3kret")
        fresh = ServiceSettings.objects.get(pk=settings.pk)
        fresh.password = "rotated"
        self.assertTrue(fresh.tracker.has_changed("password"))


class ServiceSettingsOracleTest(TestCase):
    def test_token_shaped_value_is_wrapped_not_passed_through(self):
        # A caller must not turn the field into a decryption oracle: a stolen token
        # submitted as the value is encrypted (wrapped) unconditionally, so reading it
        # back yields the injected token, not the other tenant's plaintext.
        stolen = encryption.encrypt_value("another-tenant-secret")
        settings = factories.ServiceSettingsFactory(password=stolen)

        password, _ = _raw(settings)
        self.assertNotEqual(password, stolen)  # wrapped at rest

        fresh = ServiceSettings.objects.get(pk=settings.pk)
        self.assertEqual(fresh.password, stolen)
        self.assertNotEqual(fresh.password, "another-tenant-secret")


class ServiceSettingsBackfillMigrationTest(TestCase):
    def test_backfill_encrypts_plaintext_credentials(self):
        settings = factories.ServiceSettingsFactory(password="", token=None)
        # Simulate a legacy row: plaintext credentials at rest.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE structure_servicesettings SET password=%s, token=%s WHERE id=%s",
                ["legacy-pass", "legacy-token", settings.id],
            )

        _migration.encrypt_existing_credentials(
            global_apps, SimpleNamespace(connection=connection)
        )

        password, token = _raw(settings)
        self.assertTrue(password.startswith("gAAAA"))
        self.assertTrue(token.startswith("gAAAA"))
        fresh = ServiceSettings.objects.get(pk=settings.pk)
        self.assertEqual(fresh.password, "legacy-pass")
        self.assertEqual(fresh.token, "legacy-token")


class ServiceSettingsOptionsEncryptionTest(TestCase):
    """``options`` mixes configuration with credentials; only the latter is encrypted.

    Several plugins keep a real secret in ``options`` rather than in the dedicated
    ``password`` / ``token`` columns — ``client_secret`` for the Azure service
    principal, ``keycloak_password``, ``vault_token``. Encrypting the whole blob would
    make the column unreadable for support and break nothing usefully, so the
    credential-named values are encrypted in place and everything else is left alone.
    """

    OPTIONS = {
        "client_secret": "azure-sp-secret",
        "keycloak_password": "kc-pass",
        "vault_token": "vault-tok",
        "client_id": "azure-app-id",
        "tenant_id": "azure-tenant",
        "backend_url": "https://example.com",
        "verify_ssl": True,
        "max_cpu": 32,
    }

    def setUp(self):
        self.settings = factories.ServiceSettingsFactory(options=dict(self.OPTIONS))

    def test_credential_values_are_encrypted_at_rest(self):
        raw = _raw_options(self.settings)

        for key in ("client_secret", "keycloak_password", "vault_token"):
            self.assertTrue(raw[key].startswith("gAAAA"), key)
            self.assertNotEqual(raw[key], self.OPTIONS[key])

    def test_configuration_values_stay_plaintext_at_rest(self):
        raw = _raw_options(self.settings)

        # Identifiers, endpoints and tuning flags grant no access on their own, and
        # keeping them readable preserves the column for support and debugging.
        for key in ("client_id", "tenant_id", "backend_url", "verify_ssl", "max_cpu"):
            self.assertEqual(raw[key], self.OPTIONS[key], key)

    def test_values_are_decrypted_on_read(self):
        fresh = ServiceSettings.objects.get(pk=self.settings.pk)

        self.assertEqual(fresh.options, self.OPTIONS)

    def test_unchanged_resave_is_not_a_tracked_change(self):
        fresh = ServiceSettings.objects.get(pk=self.settings.pk)

        # The attribute stays plaintext on both sides, so re-encryption under a fresh
        # Fernet IV must not look like an edit to handlers gated on a change.
        self.assertFalse(fresh.tracker.has_changed("options"))

    def test_token_shaped_value_is_wrapped_not_passed_through(self):
        # Same oracle guard as the scalar fields: a stolen ciphertext submitted as an
        # option value is wrapped, so reading it back yields the injected token.
        stolen = encryption.encrypt_value("another-tenant-secret")
        settings = factories.ServiceSettingsFactory(options={"client_secret": stolen})

        self.assertNotEqual(_raw_options(settings)["client_secret"], stolen)
        fresh = ServiceSettings.objects.get(pk=settings.pk)
        self.assertEqual(fresh.options["client_secret"], stolen)


class ServiceSettingsOptionsBackfillMigrationTest(TestCase):
    def test_backfill_encrypts_plaintext_credentials_only(self):
        settings = factories.ServiceSettingsFactory(options={})
        # Simulate a legacy row: the whole options blob in plaintext at rest.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE structure_servicesettings SET options=%s WHERE id=%s",
                [
                    json.dumps({"client_secret": "legacy-secret", "client_id": "app"}),
                    settings.id,
                ],
            )

        _options_migration.encrypt_existing_options(
            global_apps, SimpleNamespace(connection=connection)
        )

        raw = _raw_options(settings)
        self.assertTrue(raw["client_secret"].startswith("gAAAA"))
        self.assertEqual(raw["client_id"], "app")
        fresh = ServiceSettings.objects.get(pk=settings.pk)
        self.assertEqual(fresh.options["client_secret"], "legacy-secret")

    def test_backfill_is_idempotent(self):
        settings = factories.ServiceSettingsFactory(
            options={"client_secret": "already-encrypted-by-the-field"}
        )
        before = _raw_options(settings)

        _options_migration.encrypt_existing_options(
            global_apps, SimpleNamespace(connection=connection)
        )

        # A second pass must not wrap an already-encrypted value again, or the row
        # would need as many decryptions as the migration has been run.
        self.assertEqual(_raw_options(settings), before)
        fresh = ServiceSettings.objects.get(pk=settings.pk)
        self.assertEqual(
            fresh.options["client_secret"], "already-encrypted-by-the-field"
        )
