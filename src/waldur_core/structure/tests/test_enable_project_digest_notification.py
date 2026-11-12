"""Tests for the 0089 project digest notification migration.

The migration function is exercised directly against the live app registry,
mirroring core/tests/test_invitation_notification_seed.py. The module is
loaded through importlib because its name starts with a digit.
"""

from importlib import import_module

from django.apps import apps as live_apps
from rest_framework import test

from waldur_core.core.models import Notification
from waldur_core.structure.models import ProjectDigestConfiguration
from waldur_core.structure.tests import factories

migration = import_module(
    "waldur_core.structure.migrations.0089_enable_project_digest_notification"
)

KEY = migration.KEY
enable_project_digest_notification = migration.enable_project_digest_notification


class EnableProjectDigestNotificationTest(test.APITestCase):
    def setUp(self):
        Notification.objects.filter(key=KEY).delete()

    def _enable_digest(self, is_enabled=True):
        ProjectDigestConfiguration.objects.create(
            customer=factories.CustomerFactory(), is_enabled=is_enabled
        )

    def test_leaves_the_row_alone_where_no_organization_has_digests_enabled(self):
        self._enable_digest(is_enabled=False)
        Notification.objects.create(key=KEY, enabled=False)

        enable_project_digest_notification(live_apps, None)

        self.assertIs(Notification.objects.get(key=KEY).enabled, False)

    def test_turns_the_row_on_where_an_organization_has_digests_enabled(self):
        self._enable_digest()
        Notification.objects.create(key=KEY, enabled=False)

        enable_project_digest_notification(live_apps, None)

        self.assertIs(Notification.objects.get(key=KEY).enabled, True)

    def test_creates_the_row_enabled_where_it_is_missing(self):
        self._enable_digest()

        enable_project_digest_notification(live_apps, None)

        self.assertIs(Notification.objects.get(key=KEY).enabled, True)
