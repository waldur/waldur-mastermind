"""Tests for the 0042 call/proposal invitation notification seed.

The migration function is exercised directly against the live app registry
(django_test_migrations is not available), mirroring
marketplace/tests/test_enable_posix_account_backfill.py. The module is loaded
through importlib because its name starts with a digit.
"""

from importlib import import_module

from django.apps import apps as live_apps
from rest_framework import test

from waldur_core.core.models import Notification

migration = import_module(
    "waldur_core.core.migrations.0042_call_and_proposal_invitation_notifications"
)

NEW_KEYS = migration.NEW_KEYS
SOURCE_KEY = migration.SOURCE_KEY
create_invitation_notifications = migration.create_invitation_notifications


class SeedInvitationNotificationsTest(test.APITestCase):
    def setUp(self):
        # 0042 already ran against the test database; start from a clean slate.
        Notification.objects.filter(key__in=[*NEW_KEYS, SOURCE_KEY]).delete()

    def _enabled(self):
        return dict(
            Notification.objects.filter(key__in=NEW_KEYS).values_list("key", "enabled")
        )

    def test_inherits_enabled_source(self):
        Notification.objects.create(key=SOURCE_KEY, enabled=True)

        create_invitation_notifications(live_apps, None)

        self.assertEqual(self._enabled(), {key: True for key in NEW_KEYS})

    def test_inherits_disabled_source(self):
        Notification.objects.create(key=SOURCE_KEY, enabled=False)

        create_invitation_notifications(live_apps, None)

        self.assertEqual(self._enabled(), {key: False for key in NEW_KEYS})

    def test_defaults_to_disabled_without_source(self):
        # Fresh installs have no notification rows until load_notifications runs.
        create_invitation_notifications(live_apps, None)

        self.assertEqual(self._enabled(), {key: False for key in NEW_KEYS})

    def test_rerun_preserves_operator_choice(self):
        Notification.objects.create(key=SOURCE_KEY, enabled=False)
        create_invitation_notifications(live_apps, None)
        Notification.objects.filter(key=NEW_KEYS[0]).update(enabled=True)

        create_invitation_notifications(live_apps, None)

        # get_or_create must not overwrite a row an operator has since toggled.
        self.assertIs(Notification.objects.get(key=NEW_KEYS[0]).enabled, True)
