"""Tests for the 0045 access-request notification removal.

The migration function is exercised directly against the live app registry
(django_test_migrations is not available), mirroring
marketplace/tests/test_enable_posix_account_backfill.py.
"""

from django.apps import apps as live_apps
from django.test import TestCase

from waldur_core.core.migrations._remove_access_request_notification import (
    KEY,
    remove_access_request_notification,
)
from waldur_core.core.models import Notification, NotificationTemplate

TEMPLATE_PATH = "proposal/access_request_state_changed_message.txt"


class RemoveAccessRequestNotificationTest(TestCase):
    def test_removes_the_notification(self):
        Notification.objects.create(key=KEY)

        remove_access_request_notification(live_apps, None)

        self.assertFalse(Notification.objects.filter(key=KEY).exists())

    def test_leaves_the_templates_in_place(self):
        # They belong to proposal.proposal_state_changed now and are about to be
        # sent; deleting them would take any operator customisation with them.
        notification = Notification.objects.create(key=KEY)
        template = NotificationTemplate.objects.create(
            path=TEMPLATE_PATH, name=TEMPLATE_PATH
        )
        notification.templates.add(template)

        remove_access_request_notification(live_apps, None)

        self.assertTrue(
            NotificationTemplate.objects.filter(path=TEMPLATE_PATH).exists()
        )

    def test_is_a_no_op_where_the_row_was_never_created(self):
        # A deployment that never booted between the two releases.
        remove_access_request_notification(live_apps, None)

        self.assertFalse(Notification.objects.filter(key=KEY).exists())
