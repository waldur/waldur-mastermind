from datetime import timedelta

from django.utils import timezone
from django_fsm import TransitionNotAllowed
from rest_framework import test

from waldur_core.core.enums import CoreStates

from . import fixtures


class BeginTransitionIdempotencyTest(test.APITestCase):
    """A second `begin_*` call from the target state must be a no-op."""

    def setUp(self):
        super().setUp()
        self.tenant = fixtures.OpenStackFixture().tenant

    def _set_state(self, state):
        self.tenant.state = state
        self.tenant.save(update_fields=["state"])

    def test_begin_creating_from_creation_scheduled_transitions_to_creating(self):
        self._set_state(CoreStates.CREATION_SCHEDULED)
        self.tenant.begin_creating()
        self.tenant.save()
        self.assertEqual(self.tenant.state, CoreStates.CREATING)

    def test_begin_creating_from_creating_is_noop(self):
        self._set_state(CoreStates.CREATING)
        self.tenant.begin_creating()
        self.tenant.save()
        self.assertEqual(self.tenant.state, CoreStates.CREATING)

    def test_begin_updating_from_update_scheduled_transitions_to_updating(self):
        self._set_state(CoreStates.UPDATE_SCHEDULED)
        self.tenant.begin_updating()
        self.tenant.save()
        self.assertEqual(self.tenant.state, CoreStates.UPDATING)

    def test_begin_updating_from_updating_is_noop(self):
        self._set_state(CoreStates.UPDATING)
        self.tenant.begin_updating()
        self.tenant.save()
        self.assertEqual(self.tenant.state, CoreStates.UPDATING)

    def test_begin_deleting_from_deletion_scheduled_transitions_to_deleting(self):
        self._set_state(CoreStates.DELETION_SCHEDULED)
        self.tenant.begin_deleting()
        self.tenant.save()
        self.assertEqual(self.tenant.state, CoreStates.DELETING)

    def test_begin_deleting_from_deleting_is_noop(self):
        self._set_state(CoreStates.DELETING)
        self.tenant.begin_deleting()
        self.tenant.save()
        self.assertEqual(self.tenant.state, CoreStates.DELETING)

    def test_begin_creating_from_ok_still_raises(self):
        """Non-source/non-target states must still be rejected."""
        self._set_state(CoreStates.OK)
        with self.assertRaises(TransitionNotAllowed):
            self.tenant.begin_creating()


class BeginUpdatingTriggeredTimestampTest(test.APITestCase):
    """`begin_updating` refreshes `update_triggered` even on idempotent re-entry."""

    def setUp(self):
        super().setUp()
        self.tenant = fixtures.OpenStackFixture().tenant

    def test_update_triggered_set_on_first_begin_updating(self):
        self.tenant.state = CoreStates.UPDATE_SCHEDULED
        self.tenant.update_triggered = None
        self.tenant.save(update_fields=["state", "update_triggered"])

        self.tenant.begin_updating()
        self.tenant.save()

        self.assertEqual(self.tenant.state, CoreStates.UPDATING)
        self.assertIsNotNone(self.tenant.update_triggered)

    def test_update_triggered_refreshed_on_idempotent_begin_updating(self):
        old = timezone.now() - timedelta(hours=1)
        self.tenant.state = CoreStates.UPDATING
        self.tenant.update_triggered = old
        self.tenant.save(update_fields=["state", "update_triggered"])

        self.tenant.begin_updating()
        self.tenant.save()

        self.assertEqual(self.tenant.state, CoreStates.UPDATING)
        self.assertGreater(self.tenant.update_triggered, old)
