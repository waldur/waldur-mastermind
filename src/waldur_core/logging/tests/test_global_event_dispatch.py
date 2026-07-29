"""End-to-end tests for global (empty-scope) EventConsumer delivery of
user-centric events: profile, SSH keys, lifecycle, and roles on any scope.

These exercise the real signal wiring in logging/apps.py by saving User /
SshPublicKey / role rows and asserting publish_messages.delay receives the
right consumer_{uuid} messages. No marketplace involvement.
"""

import datetime
import json
from unittest import mock

from constance.test import override_config
from rest_framework import test

from waldur_core.core.models import SshPublicKey
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories

DELAY = "waldur_core.logging.tasks.publish_messages.delay"


def _messages(mock_delay):
    msgs = []
    for call in mock_delay.call_args_list:
        msgs.extend(call.args[0])
    return msgs


def _payloads(mock_delay):
    return [json.loads(m["payload"]) for m in _messages(mock_delay)]


class GlobalEventDispatchTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.consumer = logging_factories.EventConsumerFactory(
            user=self.staff,
            queue_created=True,
            rmq_username="aaaa0000000000000000000000000001",
        )
        self.topic = f"consumer_{self.consumer.uuid.hex}"

    # ---- USER_PROFILE ----

    @mock.patch(DELAY)
    def test_profile_change_delivers(self, mock_delay):
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()  # ignore the lifecycle 'created' event
        user.email = "changed@example.com"
        user.save()

        msgs = _messages(mock_delay)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["topic"], self.topic)
        self.assertEqual(msgs[0]["vhost"], self.staff.uuid.hex)
        payload = json.loads(msgs[0]["payload"])
        self.assertEqual(payload["object_type"], "user_profile")
        self.assertIn("email", payload["changed"])
        self.assertEqual(payload["changed"]["email"][1], "changed@example.com")

    @mock.patch(DELAY)
    def test_untracked_field_change_delivers_nothing(self, mock_delay):
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()
        user.description = "some note"  # not a broadcast profile field
        user.save()
        self.assertEqual(_payloads(mock_delay), [])

    @mock.patch(DELAY)
    def test_is_active_change_is_lifecycle_not_profile(self, mock_delay):
        user = structure_factories.UserFactory(is_active=True)
        mock_delay.reset_mock()
        user.is_active = False
        user.save()

        types = [p["object_type"] for p in _payloads(mock_delay)]
        self.assertIn("user_lifecycle", types)
        self.assertNotIn("user_profile", types)

    # ---- USER_PROFILE: GDPR / enabled-attributes gating ----

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=[])
    @mock.patch(DELAY)
    def test_disabled_profile_field_not_broadcast(self, mock_delay):
        """A configurable field the platform has disabled must NOT be signalled
        (data minimization): changing `organization` while it is disabled emits
        no user_profile event."""
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()
        user.organization = "Acme Corp"
        user.save()
        self.assertEqual(
            [p for p in _payloads(mock_delay) if p["object_type"] == "user_profile"],
            [],
        )

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["organization"])
    @mock.patch(DELAY)
    def test_enabled_profile_field_is_broadcast(self, mock_delay):
        """Once the operator enables the attribute, its changes are signalled."""
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()
        user.organization = "Acme Corp"
        user.save()
        profile = [
            p for p in _payloads(mock_delay) if p["object_type"] == "user_profile"
        ]
        self.assertEqual(len(profile), 1)
        self.assertIn("organization", profile[0]["changed"])

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=[])
    @mock.patch(DELAY)
    def test_core_field_always_broadcast(self, mock_delay):
        """Core attributes (email) are always enabled regardless of config."""
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()
        user.email = "core@example.com"
        user.save()
        profile = [
            p for p in _payloads(mock_delay) if p["object_type"] == "user_profile"
        ]
        self.assertEqual(len(profile), 1)
        self.assertIn("email", profile[0]["changed"])

    # ---- USER_LIFECYCLE ----

    @mock.patch(DELAY)
    def test_lifecycle_created_and_deactivated(self, mock_delay):
        user = structure_factories.UserFactory(is_active=True)
        created = [
            p for p in _payloads(mock_delay) if p["object_type"] == "user_lifecycle"
        ]
        self.assertTrue(any(p["action"] == "created" for p in created))

        mock_delay.reset_mock()
        user.is_active = False
        user.save()
        actions = [
            p["action"]
            for p in _payloads(mock_delay)
            if p["object_type"] == "user_lifecycle"
        ]
        self.assertEqual(actions, ["deactivated"])

    @mock.patch(DELAY)
    def test_lifecycle_delete(self, mock_delay):
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()
        user.delete()
        actions = [
            p["action"]
            for p in _payloads(mock_delay)
            if p["object_type"] == "user_lifecycle"
        ]
        self.assertEqual(actions, ["deleted"])

    # ---- USER_SSH_KEY ----

    @mock.patch(DELAY)
    def test_ssh_key_add_and_remove(self, mock_delay):
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()
        key = structure_factories.SshPublicKeyFactory(user=user)
        added = [p for p in _payloads(mock_delay) if p["object_type"] == "user_ssh_key"]
        self.assertTrue(added)
        self.assertEqual(added[0]["action"], "added")
        self.assertEqual(added[0]["user_uuid"], user.uuid.hex)

        mock_delay.reset_mock()
        SshPublicKey.objects.filter(pk=key.pk).first().delete()
        removed = [
            p for p in _payloads(mock_delay) if p["object_type"] == "user_ssh_key"
        ]
        self.assertTrue(removed)
        self.assertEqual(removed[0]["action"], "removed")

    # ---- USER_ROLE (any scope, even without site-agent resources) ----

    @mock.patch(DELAY)
    def test_role_granted_on_plain_project(self, mock_delay):
        project = structure_factories.ProjectFactory()  # no site-agent resources
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()
        project.add_user(user, ProjectRole.MEMBER)

        role_msgs = [
            p for p in _payloads(mock_delay) if p["object_type"] == "user_role"
        ]
        self.assertTrue(role_msgs)
        self.assertTrue(role_msgs[0]["granted"])
        self.assertEqual(role_msgs[0]["scope_type"], "project")
        self.assertEqual(role_msgs[0]["user_uuid"], user.uuid.hex)

    # ---- object_types allow-list ----

    @mock.patch(DELAY)
    def test_object_types_allow_list_filters(self, mock_delay):
        self.consumer.object_types = ["user_role"]
        self.consumer.save(update_fields=["object_types"])
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()
        user.email = "x@example.com"
        user.save()
        # user_profile is not in the allow-list → nothing delivered.
        self.assertEqual(_payloads(mock_delay), [])

    # ---- isolation / no cross-delivery ----

    @mock.patch(DELAY)
    def test_offering_scoped_consumer_gets_no_global_event(self, mock_delay):
        from waldur_mastermind.marketplace.tests import factories as mp_factories

        offering = mp_factories.OfferingFactory()
        logging_factories.EventConsumerFactory.for_offering(
            offering,
            user=structure_factories.UserFactory(),
            queue_created=True,
            rmq_username="bbbb0000000000000000000000000002",
        )
        # Delete the global consumer so only the offering-scoped one remains.
        self.consumer.delete()
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()
        user.email = "y@example.com"
        user.save()
        self.assertEqual(_payloads(mock_delay), [])

    # ---- delivery-time re-authorization (privilege revocation) ----

    @mock.patch(DELAY)
    def test_demoted_owner_stops_receiving(self, mock_delay):
        """If the consumer owner loses staff/support (but stays active), delivery
        stops immediately — the registration guard is re-checked at publish."""
        self.staff.is_staff = False
        self.staff.is_support = False
        self.staff.save()
        mock_delay.reset_mock()

        user = structure_factories.UserFactory()
        user.email = "after-demotion@example.com"
        user.save()
        self.assertEqual(_payloads(mock_delay), [])

    @mock.patch(DELAY)
    def test_inactive_owner_stops_receiving(self, mock_delay):
        self.staff.is_active = False
        self.staff.save()
        mock_delay.reset_mock()

        user = structure_factories.UserFactory()
        user.email = "after-deactivation@example.com"
        user.save()
        self.assertEqual(_payloads(mock_delay), [])

    # ---- side-effect guard ----

    @mock.patch("waldur_core.logging.event_dispatch.get_skip_side_effects")
    @mock.patch(DELAY)
    def test_skip_side_effects_suppresses(self, mock_delay, mock_skip):
        mock_skip.return_value = True
        user = structure_factories.UserFactory()
        user.email = "z@example.com"
        user.save()
        self.assertEqual(_payloads(mock_delay), [])

    @mock.patch(DELAY)
    def test_no_global_consumer_is_a_noop(self, mock_delay):
        self.consumer.delete()
        user = structure_factories.UserFactory()
        user.email = "n@example.com"
        user.save()
        self.assertEqual(_payloads(mock_delay), [])

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["birth_date"])
    @mock.patch(DELAY)
    def test_non_json_profile_value_does_not_break_the_save(self, mock_delay):
        """A date-typed profile attribute must not blow up the user's save.

        The watched set is config-driven, so it can include non-string columns
        (birth_date is a DateField). Serializing the diff with the stdlib json
        encoder raised TypeError *inside* the post_save handler, aborting the
        save and making the profile impossible to edit.
        """
        user = structure_factories.UserFactory()
        mock_delay.reset_mock()
        user.birth_date = datetime.date(1990, 1, 2)
        user.save()  # must not raise

        profile = [
            p for p in _payloads(mock_delay) if p["object_type"] == "user_profile"
        ]
        self.assertEqual(len(profile), 1)
        self.assertEqual(profile[0]["changed"]["birth_date"][1], "1990-01-02")


class SelfScopedEventDispatchTest(test.APITestCase):
    """Delivery to a consumer bound to its own user (the self-referential
    ``user`` scope): identity authorizes delivery, and only the affected
    user's own consumer matches."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.user,
            user=self.user,
            queue_created=True,
            rmq_username="cccc0000000000000000000000000003",
        )
        self.topic = f"consumer_{self.consumer.uuid.hex}"

    def _own(self, mock_delay):
        return [
            json.loads(m["payload"])
            for m in _messages(mock_delay)
            if m["topic"] == self.topic
        ]

    @mock.patch(DELAY)
    def test_own_profile_change_delivers(self, mock_delay):
        self.user.email = "self@example.com"
        self.user.save()
        payloads = self._own(mock_delay)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["object_type"], "user_profile")
        self.assertEqual(payloads[0]["user_uuid"], self.user.uuid.hex)

    @mock.patch(DELAY)
    def test_other_users_events_are_not_delivered(self, mock_delay):
        other = structure_factories.UserFactory()
        mock_delay.reset_mock()
        other.email = "other@example.com"
        other.save()
        self.assertEqual(self._own(mock_delay), [])

    @mock.patch(DELAY)
    def test_own_ssh_key_delivers(self, mock_delay):
        key = structure_factories.SshPublicKeyFactory(user=self.user)
        payloads = [
            p for p in self._own(mock_delay) if p["object_type"] == "user_ssh_key"
        ]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["action"], "added")
        self.assertEqual(payloads[0]["ssh_key_uuid"], key.uuid.hex)

    @mock.patch(DELAY)
    def test_own_role_grant_delivers(self, mock_delay):
        project = structure_factories.ProjectFactory()
        mock_delay.reset_mock()
        project.add_user(self.user, ProjectRole.MEMBER)
        payloads = [p for p in self._own(mock_delay) if p["object_type"] == "user_role"]
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["granted"])

    @mock.patch(DELAY)
    def test_object_types_allow_list_applies(self, mock_delay):
        self.consumer.object_types = ["user_ssh_key"]
        self.consumer.save(update_fields=["object_types"])
        self.user.email = "filtered@example.com"
        self.user.save()
        self.assertEqual(self._own(mock_delay), [])

    @mock.patch(DELAY)
    def test_global_consumer_still_receives_alongside_self(self, mock_delay):
        staff = structure_factories.UserFactory(is_staff=True)
        global_consumer = logging_factories.EventConsumerFactory(
            user=staff,
            queue_created=True,
            rmq_username="dddd0000000000000000000000000004",
        )
        mock_delay.reset_mock()
        self.user.email = "both@example.com"
        self.user.save()
        topics = [m["topic"] for m in _messages(mock_delay)]
        self.assertIn(self.topic, topics)
        self.assertIn(f"consumer_{global_consumer.uuid.hex}", topics)
        # Exactly one message each — no duplicates.
        self.assertEqual(len(topics), 2)

    @mock.patch(DELAY)
    def test_mixed_binding_receives_user_events_via_identity(self, mock_delay):
        """A consumer bound to [project, self] gets the user events through the
        identity branch even though no UserRole authorizes the user scope-key."""
        project = structure_factories.ProjectFactory()
        project.add_user(self.user, ProjectRole.MEMBER)
        mixed = logging_factories.EventConsumerFactory.with_scopes(
            project,
            self.user,
            user=self.user,
            queue_created=True,
            rmq_username="eeee0000000000000000000000000005",
        )
        # Two consumers of the same user now exist; drop the plain self one to
        # isolate the mixed consumer.
        self.consumer.delete()
        mock_delay.reset_mock()
        self.user.email = "mixed@example.com"
        self.user.save()
        topics = [m["topic"] for m in _messages(mock_delay)]
        self.assertEqual(topics, [f"consumer_{mixed.uuid.hex}"])

    @mock.patch(DELAY)
    def test_deactivated_owner_stops_receiving(self, mock_delay):
        self.user.is_active = False
        self.user.save()
        mock_delay.reset_mock()
        self.user.email = "gone@example.com"
        self.user.save()
        self.assertEqual(self._own(mock_delay), [])


class StaffTrackedUserDispatchTest(test.APITestCase):
    """Staff/support may bind a consumer to OTHER users (a targeted,
    data-minimized alternative to the global firehose): they receive exactly
    the tracked users' identity events, and lose delivery on demotion."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.tracked = structure_factories.UserFactory()
        self.untracked = structure_factories.UserFactory()
        self.consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.tracked,
            user=self.staff,
            queue_created=True,
            rmq_username="ffff0000000000000000000000000006",
        )
        self.topic = f"consumer_{self.consumer.uuid.hex}"

    def _own(self, mock_delay):
        return [
            json.loads(m["payload"])
            for m in _messages(mock_delay)
            if m["topic"] == self.topic
        ]

    @mock.patch(DELAY)
    def test_tracked_users_events_are_delivered(self, mock_delay):
        self.tracked.email = "tracked@example.com"
        self.tracked.save()
        payloads = self._own(mock_delay)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["user_uuid"], self.tracked.uuid.hex)

    @mock.patch(DELAY)
    def test_untracked_users_events_are_not_delivered(self, mock_delay):
        self.untracked.email = "untracked@example.com"
        self.untracked.save()
        self.assertEqual(self._own(mock_delay), [])

    @mock.patch(DELAY)
    def test_set_of_users_binding(self, mock_delay):
        multi = logging_factories.EventConsumerFactory.with_scopes(
            self.tracked,
            self.untracked,
            user=self.staff,
            queue_created=True,
            rmq_username="0aaa0000000000000000000000000007",
        )
        topic = f"consumer_{multi.uuid.hex}"
        for user in (self.tracked, self.untracked):
            mock_delay.reset_mock()
            user.email = f"{user.username}@multi.example.com"
            user.save()
            topics = [m["topic"] for m in _messages(mock_delay)]
            self.assertIn(topic, topics)

    @mock.patch(DELAY)
    def test_demoted_owner_stops_receiving_tracked_events(self, mock_delay):
        self.staff.is_staff = False
        self.staff.save()
        mock_delay.reset_mock()
        self.tracked.email = "after-demotion@example.com"
        self.tracked.save()
        self.assertEqual(self._own(mock_delay), [])
