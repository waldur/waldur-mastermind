"""Tests for core.0062, which keeps who has personal access tokens unchanged
when PAT_ENABLED stops being required alongside the per-user flag.

The migration function is exercised directly against the live app registry,
as in configuration/tests/test_removed_reporting_screens.py.
"""

import importlib

from django.apps import apps
from django.test import TestCase
from rest_framework.exceptions import AuthenticationFailed

from waldur_core.configuration.models import Setting
from waldur_core.core.authentication import authenticate_personal_access_token
from waldur_core.core.tests.test_personal_access_tokens import _create_pat
from waldur_core.structure.tests import factories

migration = importlib.import_module(
    "waldur_core.core.migrations.0062_preserve_pat_access"
)


class PreservePatAccessTest(TestCase):
    def _store(self, value, **extra):
        return Setting.objects.create(
            key=migration.KEY, value=value, source="api", **extra
        )

    def _run(self):
        migration.preserve_pat_access(apps, None)

    def test_a_stored_true_is_turned_off(self):
        self._store(True)
        self._run()
        setting = Setting.objects.get(key=migration.KEY)
        self.assertIs(setting.value, False)
        self.assertEqual(setting.source, "migration")
        self.assertEqual(setting.source_detail, migration.SOURCE_DETAIL)

    def test_a_stored_false_is_left_alone(self):
        self._store(False)
        self._run()
        setting = Setting.objects.get(key=migration.KEY)
        self.assertIs(setting.value, False)
        self.assertEqual(setting.source, "api")

    def test_no_row_is_created_where_none_was_stored(self):
        self._run()
        self.assertFalse(Setting.objects.filter(key=migration.KEY).exists())

    def test_a_deployment_managed_value_is_turned_off_and_reported(self):
        self._store(True, locked=True, managed_by="/etc/waldur/override.yaml")
        with self.assertLogs(migration.logger, level="WARNING") as logs:
            self._run()
        self.assertIs(Setting.objects.get(key=migration.KEY).value, False)
        self.assertIn("/etc/waldur/override.yaml", logs.output[0])

    def test_the_same_users_have_tokens_before_and_after(self):
        # Before the upgrade: PAT_ENABLED on, and only flagged users have tokens.
        self._store(True)
        flagged = factories.UserFactory(can_use_personal_access_tokens=True)
        unflagged = factories.UserFactory(can_use_personal_access_tokens=False)
        flagged_pat = _create_pat(flagged)
        unflagged_pat = _create_pat(unflagged)

        # Without the migration, the new rule would let the unflagged user in.
        user, _ = authenticate_personal_access_token(
            unflagged_pat._plaintext_token, "10.0.0.1"
        )
        self.assertEqual(user, unflagged)

        self._run()

        user, _ = authenticate_personal_access_token(
            flagged_pat._plaintext_token, "10.0.0.1"
        )
        self.assertEqual(user, flagged)
        with self.assertRaises(AuthenticationFailed):
            authenticate_personal_access_token(
                unflagged_pat._plaintext_token, "10.0.0.1"
            )
