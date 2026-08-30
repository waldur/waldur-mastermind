"""Tests for the 0046 workflow-step-event notification seed.

Exercised directly against the live app registry, mirroring
test_invitation_notification_seed.py.
"""

from importlib import import_module

from django.apps import apps as live_apps
from rest_framework import test

from waldur_core.core.models import Notification

migration = import_module(
    "waldur_core.core.migrations.0046_workflow_step_event_notification"
)

KEY = migration.KEY
create_notification = migration.create_notification


class SeedWorkflowStepNotificationTest(test.APITestCase):
    def setUp(self):
        Notification.objects.all().delete()

    def test_skips_fresh_database(self):
        # migrate_fresh never runs this migration; migrate must match it.
        create_notification(live_apps, None)

        self.assertFalse(Notification.objects.exists())

    def test_creates_enabled_on_existing_deployment(self):
        Notification.objects.create(key="users.invitation_created", enabled=False)

        create_notification(live_apps, None)

        self.assertIs(Notification.objects.get(key=KEY).enabled, True)

    def test_rerun_preserves_operator_choice(self):
        Notification.objects.create(key="users.invitation_created", enabled=False)
        Notification.objects.create(key=KEY, enabled=False)

        create_notification(live_apps, None)

        self.assertIs(Notification.objects.get(key=KEY).enabled, False)
