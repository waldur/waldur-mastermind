"""Tests for the 0043 project usage update notification migration.

The migration function is exercised directly against the live app registry,
as structure/tests/test_enable_project_digest_notification.py does. The
module is loaded through importlib because its name starts with a digit.
"""

from importlib import import_module

from django.apps import apps as live_apps
from rest_framework import test

from waldur_core.core.models import Notification
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openportal import models

migration = import_module(
    "waldur_openportal.migrations.0043_enable_project_usage_update_notification"
)

KEY = migration.KEY
enable = migration.enable_project_usage_update_notification


class EnableProjectUsageUpdateNotificationTest(test.APITestCase):
    def setUp(self):
        Notification.objects.filter(key=KEY).delete()
        self.fixture = structure_fixtures.ProjectFixture()

    def _allocation(self, is_active=True):
        models.Allocation.objects.create(
            name="local",
            project=self.fixture.project,
            service_settings=structure_factories.ServiceSettingsFactory(
                customer=self.fixture.customer
            ),
            is_active=is_active,
        )

    def test_leaves_the_row_alone_where_no_allocation_is_active(self):
        self._allocation(is_active=False)
        Notification.objects.create(key=KEY, enabled=False)

        enable(live_apps, None)

        self.assertIs(Notification.objects.get(key=KEY).enabled, False)

    def test_does_not_create_the_row_where_openportal_is_not_used(self):
        enable(live_apps, None)

        self.assertFalse(Notification.objects.filter(key=KEY).exists())

    def test_turns_the_row_on_where_an_allocation_is_active(self):
        self._allocation()
        Notification.objects.create(key=KEY, enabled=False)

        enable(live_apps, None)

        self.assertIs(Notification.objects.get(key=KEY).enabled, True)

    def test_creates_the_row_enabled_where_it_is_missing(self):
        self._allocation()

        enable(live_apps, None)

        self.assertIs(Notification.objects.get(key=KEY).enabled, True)
