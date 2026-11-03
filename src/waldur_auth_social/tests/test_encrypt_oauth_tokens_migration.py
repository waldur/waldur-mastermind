"""Tests for migration 0018_encrypt_oauth_tokens: existing plaintext refresh
tokens and client secrets are encrypted, and encrypted ones are left alone."""

from importlib import import_module
from types import SimpleNamespace

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, override_settings

from waldur_auth_social import models
from waldur_core.core import encryption
from waldur_core.structure.tests import factories as structure_factories

# The module name starts with a digit, so it cannot be imported by name.
migration = import_module("waldur_auth_social.migrations.0018_encrypt_oauth_tokens")

MIGRATION = ("waldur_auth_social", "0018_encrypt_oauth_tokens")


def raw(model, column, pk):
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {column} FROM {model._meta.db_table} WHERE id = %s",  # noqa: S608
            [pk],
        )
        return cursor.fetchone()[0]


def write_raw(model, column, pk, value):
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {model._meta.db_table} SET {column} = %s WHERE id = %s",  # noqa: S608
            [value, pk],
        )


# pytest runs with --no-migrations, which empties MIGRATION_MODULES and with it
# the loader; the historical registry is built from the migration files.
@override_settings(MIGRATION_MODULES={})
class EncryptOAuthTokensMigrationTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.historical_apps = MigrationLoader(None).project_state(MIGRATION).apps

    def setUp(self):
        self.provider = models.IdentityProvider.objects.create(
            provider="keycloak", client_id="waldur", label="Keycloak"
        )
        self.token = models.OAuthToken.objects.create(
            user=structure_factories.UserFactory(), provider="keycloak"
        )
        # Rows as the previous schema left them: plaintext.
        write_raw(models.IdentityProvider, "client_secret", self.provider.pk, "secret")
        write_raw(models.OAuthToken, "refresh_token", self.token.pk, "refresh")

    def _run(self):
        migration.encrypt_existing_credentials(
            self.historical_apps, SimpleNamespace(connection=connection)
        )

    def test_plaintext_values_are_encrypted(self):
        self._run()
        secret = raw(models.IdentityProvider, "client_secret", self.provider.pk)
        refresh = raw(models.OAuthToken, "refresh_token", self.token.pk)
        self.assertEqual(encryption.decrypt_value(secret), "secret")
        self.assertEqual(encryption.decrypt_value(refresh), "refresh")

    def test_running_twice_does_not_encrypt_twice(self):
        self._run()
        first = raw(models.OAuthToken, "refresh_token", self.token.pk)
        self._run()
        self.assertEqual(raw(models.OAuthToken, "refresh_token", self.token.pk), first)
