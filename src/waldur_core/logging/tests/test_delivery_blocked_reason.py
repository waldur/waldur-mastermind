"""Why an operator sees nothing arriving on a consumer queue.

Every rung below is a condition under which build_messages silently drops the
consumer — no log line, no exception, no state on the row. delivery_blocked_reason
turns each of them into something an operator can read.
"""

from rest_framework import test

from waldur_core.logging import event_dispatch
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions import models as permission_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

RMQ = "aabb000000000000000000000000ccdd"


class DeliveryBlockedReasonTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project

    def _bound_consumer(self, user, **kwargs):
        return logging_factories.EventConsumerFactory.with_scopes(
            self.project,
            user=user,
            queue_created=True,
            rmq_username=RMQ,
            **kwargs,
        )

    def _global_consumer(self, user, **kwargs):
        return logging_factories.EventConsumerFactory.with_scopes(
            user=user, queue_created=True, rmq_username=RMQ, **kwargs
        )

    def test_a_working_consumer_reports_nothing(self):
        consumer = self._bound_consumer(self.fixture.manager)
        self.assertIsNone(event_dispatch.delivery_blocked_reason(consumer))

    def test_deactivated_owner_is_reported(self):
        manager = self.fixture.manager
        consumer = self._bound_consumer(manager)
        manager.is_active = False
        manager.save(update_fields=["is_active"])

        self.assertIn("deactivated", event_dispatch.delivery_blocked_reason(consumer))

    def test_unprovisioned_queue_is_reported(self):
        consumer = self._bound_consumer(self.fixture.manager)
        consumer.queue_created = False

        self.assertIn(
            "not provisioned", event_dispatch.delivery_blocked_reason(consumer)
        )

    def test_missing_rmq_credential_is_reported(self):
        consumer = self._bound_consumer(self.fixture.manager)
        consumer.rmq_username = ""

        self.assertIn(
            "RabbitMQ credential", event_dispatch.delivery_blocked_reason(consumer)
        )

    def test_demoted_global_owner_is_reported(self):
        """A global consumer is staff/support only; demotion silently empties it."""
        staff = structure_factories.UserFactory(is_staff=True)
        consumer = self._global_consumer(staff)
        self.assertIsNone(event_dispatch.delivery_blocked_reason(consumer))

        staff.is_staff = False
        staff.save(update_fields=["is_staff"])
        consumer.refresh_from_db()

        self.assertIn(
            "no longer staff/support",
            event_dispatch.delivery_blocked_reason(consumer),
        )

    def test_owner_losing_their_role_is_reported(self):
        manager = self.fixture.manager
        consumer = self._bound_consumer(manager)

        permission_models.UserRole.objects.filter(user=manager).update(is_active=False)

        self.assertIn("holds no role", event_dispatch.delivery_blocked_reason(consumer))

    def test_owner_who_never_held_a_role_is_reported(self):
        """register_queue also admits an identity manager, who holds no role on
        the offering — dispatch drops them just the same, so the message must not
        claim a role was lost."""
        outsider = structure_factories.UserFactory()
        consumer = self._bound_consumer(outsider)

        self.assertIn("holds no role", event_dispatch.delivery_blocked_reason(consumer))

    def test_dangling_bindings_are_reported_distinctly(self):
        """A binding whose target row is gone resolves to None; re-registering is
        the fix, not restoring a role.

        Built by hand: EventConsumerScope is a GenericForeignKey with no
        constraint, and Project deletion is soft, so the only way to reach this
        state is a hard-deleted (or never-existing) target.
        """
        manager = self.fixture.manager
        consumer = self._bound_consumer(manager)
        binding = consumer.scopes.get()
        binding.object_id = 10**9
        binding.save(update_fields=["object_id"])

        self.assertIn(
            "no longer exist", event_dispatch.delivery_blocked_reason(consumer)
        )

    def test_binding_to_another_user_is_not_reported_as_dangling(self):
        """Staff may bind to someone else's identity; after demotion no role can
        authorize that binding, but the users still exist — the reason must be
        about the role, not about missing rows."""
        owner = structure_factories.UserFactory()
        other = structure_factories.UserFactory()
        consumer = logging_factories.EventConsumerFactory.with_scopes(
            other, user=owner, queue_created=True, rmq_username=RMQ
        )

        self.assertIn("holds no role", event_dispatch.delivery_blocked_reason(consumer))

    def test_dead_object_type_filter_is_reported(self):
        """An allow-list of types that no longer exist matches nothing, and does
        so after authorization — so even a staff owner receives nothing."""
        staff = structure_factories.UserFactory(is_staff=True)
        consumer = self._bound_consumer(staff, object_types=["renamed_away"])

        self.assertIn("still exists", event_dispatch.delivery_blocked_reason(consumer))

    def test_partially_live_object_type_filter_is_not_reported(self):
        """One live type is enough — whether an event of that type ever reaches
        this consumer is a per-event question the row cannot answer."""
        consumer = self._bound_consumer(
            self.fixture.manager, object_types=["renamed_away", "order"]
        )

        self.assertIsNone(event_dispatch.delivery_blocked_reason(consumer))

    def test_self_bound_consumer_is_authorized_by_identity(self):
        """A user's own identity binding needs no role at all."""
        user = structure_factories.UserFactory()
        consumer = logging_factories.EventConsumerFactory.with_scopes(
            user, user=user, queue_created=True, rmq_username=RMQ
        )

        self.assertIsNone(event_dispatch.delivery_blocked_reason(consumer))

    def test_staff_owner_of_a_bound_consumer_is_never_blocked(self):
        """Staff skip the delivery re-auth entirely, so a role is irrelevant."""
        staff = structure_factories.UserFactory(is_staff=True)
        consumer = self._bound_consumer(staff)

        self.assertIsNone(event_dispatch.delivery_blocked_reason(consumer))
