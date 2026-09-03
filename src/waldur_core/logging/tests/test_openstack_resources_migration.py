"""Tests for migration 0028_split_openstack_resource_event_groups.

OpenStack event types moved out of the generic `resources` group. Every
subscription that named `resources` must keep delivering exactly what it
delivered before the split.
"""

from importlib import import_module

from django.apps import apps as global_apps
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.logging import models
from waldur_core.logging.enums import EVENT_GROUP_MAPPING, EventGroup
from waldur_core.logging.event_logger import expand_event_groups
from waldur_core.logging.tests import factories
from waldur_core.structure.tests import factories as structure_factories

# The module name starts with a digit, so it cannot be imported by name.
migration = import_module(
    "waldur_core.logging.migrations.0028_split_openstack_resource_event_groups"
)

# What `resources` expanded to before the split.
PRE_SPLIT_RESOURCES = {
    event.value
    for event in EVENT_GROUP_MAPPING[EventGroup.RESOURCES]
    + EVENT_GROUP_MAPPING[EventGroup.OPENSTACK_RESOURCES]
}


class SplitOpenstackResourcesMigrationTest(TestCase):
    def _run(self):
        migration.add_openstack_resources(global_apps, None)

    def test_system_notification_keeps_delivering_openstack_events(self):
        """SystemNotification expands its groups on every dispatch, so without
        the migration the split would narrow delivery immediately.
        """
        notification = models.SystemNotification.objects.create(
            hook_content_type=ContentType.objects.get_for_model(models.EmailHook),
            name="test-notification",
            event_types=[],
            event_groups=["resources"],
            roles=["admin"],
        )

        self._run()

        notification.refresh_from_db()
        self.assertEqual(
            PRE_SPLIT_RESOURCES, set(expand_event_groups(notification.event_groups))
        )

    def test_hook_event_groups_survive_a_later_update(self):
        """A hook persists expanded event types, so it keeps delivering until
        something re-expands its groups. The migration keeps that re-expansion
        from dropping the OpenStack half.
        """
        hook = factories.WebHookFactory(
            user=structure_factories.UserFactory(),
            event_groups=["resources"],
            event_types=sorted(PRE_SPLIT_RESOURCES),
        )

        self._run()

        hook.refresh_from_db()
        self.assertIn("openstack_resources", hook.event_groups)
        self.assertEqual(
            PRE_SPLIT_RESOURCES, set(expand_event_groups(hook.event_groups))
        )

    def test_unrelated_subscription_is_left_alone(self):
        hook = factories.EmailHookFactory(
            user=structure_factories.UserFactory(),
            event_groups=["auth", "users"],
        )

        self._run()

        hook.refresh_from_db()
        self.assertEqual(["auth", "users"], hook.event_groups)

    def test_is_idempotent(self):
        hook = factories.WebHookFactory(
            user=structure_factories.UserFactory(),
            event_groups=["resources"],
        )

        self._run()
        self._run()

        hook.refresh_from_db()
        self.assertEqual(["resources", "openstack_resources"], hook.event_groups)

    def test_reverse_leaves_subscriptions_alone(self):
        """Reversing must not unsubscribe anyone.

        `resources` + `openstack_resources` delivers what `resources` delivered
        before the split, and the reverse cannot tell that pairing from one a
        user chose deliberately.
        """
        hook = factories.WebHookFactory(
            user=structure_factories.UserFactory(),
            event_groups=["resources"],
        )

        self._run()
        migration.drop_openstack_resources(global_apps, None)

        hook.refresh_from_db()
        self.assertEqual(["resources", "openstack_resources"], hook.event_groups)
        self.assertEqual(
            PRE_SPLIT_RESOURCES, set(expand_event_groups(hook.event_groups))
        )
