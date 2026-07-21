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
