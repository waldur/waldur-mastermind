import importlib
import json

from constance.test import override_config
from django.apps import apps
from django.db import connection
from django.test import TestCase
from rest_framework import status, test

from waldur_core.structure.tests.factories import UserFactory

migration = importlib.import_module(
    "waldur_core.core.migrations.0052_rename_matrix_login_method"
)


def stored(value):
    return json.dumps({"__type__": "default", "__value__": value})


class RenameMatrixLoginMethodMigrationTest(TestCase):
    def _set(self, key, value):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM constance_constance WHERE key = %s", [key])
            cursor.execute(
                "INSERT INTO constance_constance (key, value) VALUES (%s, %s)",
                [key, value],
            )

    def _get(self, key):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT value FROM constance_constance WHERE key = %s", [key]
            )
            row = cursor.fetchone()
        return json.loads(row[0])["__value__"] if row else None

    def _run(self):
        with connection.schema_editor() as schema_editor:
            migration.rename_login_method(apps, schema_editor)

    def test_password_and_oidc_carry_over(self):
        for method in ("password", "oidc"):
            self._set(migration.OLD_KEY, stored(method))

            self._run()

            self.assertEqual(self._get(migration.NEW_KEY), method)
            self.assertIsNone(self._get(migration.OLD_KEY))

    def test_token_becomes_none(self):
        # No external client can sign in with the appservice token that the
        # token method handed out, so it has no successor.
        self._set(migration.OLD_KEY, stored("token"))

        self._run()

        self.assertEqual(self._get(migration.NEW_KEY), "none")

    def test_stored_value_wins_over_an_auto_created_new_key(self):
        self._set(migration.OLD_KEY, stored("password"))
        self._set(migration.NEW_KEY, stored("none"))

        self._run()

        self.assertEqual(self._get(migration.NEW_KEY), "password")

    def test_malformed_values_become_none(self):
        for raw in ("not json", json.dumps({"__value__": ["password"]}), "null"):
            self._set(migration.OLD_KEY, raw)

            self._run()

            self.assertEqual(self._get(migration.NEW_KEY), "none", raw)

    def test_unknown_method_becomes_none(self):
        self._set(migration.OLD_KEY, stored("ldap"))

        self._run()

        self.assertEqual(self._get(migration.NEW_KEY), "none")

    def test_nothing_stored_leaves_the_default(self):
        self._run()

        self.assertIsNone(self._get(migration.NEW_KEY))

    def test_drops_the_oidc_provider_url(self):
        self._set("MATRIX_OIDC_PROVIDER_URL", stored("https://idp.example.com"))

        self._run()

        self.assertIsNone(self._get("MATRIX_OIDC_PROVIDER_URL"))


class MatrixExternalLoginMethodSettingTest(test.APITestCase):
    url = "/api/override-settings/"

    def setUp(self):
        self.client.force_authenticate(UserFactory(is_staff=True))

    def test_defaults_to_none(self):
        response = self.client.get(self.url)

        self.assertEqual(response.data["MATRIX_EXTERNAL_LOGIN_METHOD"], "none")

    def test_accepts_supported_methods(self):
        for method in ("none", "password", "oidc"):
            response = self.client.post(
                self.url, {"MATRIX_EXTERNAL_LOGIN_METHOD": method}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, method)

    def test_rejects_the_removed_token_method(self):
        response = self.client.post(self.url, {"MATRIX_EXTERNAL_LOGIN_METHOD": "token"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(MATRIX_EXTERNAL_LOGIN_METHOD="oidc")
    def test_is_public(self):
        self.client.logout()

        response = self.client.get("/api/configuration/")

        self.assertEqual(
            response.data["WALDUR_CORE"]["MATRIX_EXTERNAL_LOGIN_METHOD"], "oidc"
        )
