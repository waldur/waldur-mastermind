"""Regression tests for the action → EventType lookup in the openstack
post_save handler.

The rescue endpoint crashed in production with::

    ValueError: 'resource_rescue_scheduled' is not a valid EventType

because :class:`waldur_openstack.executors.InstanceRescueExecutor` (and its
``Unrescue`` sibling) was added without the corresponding
``RESOURCE_RESCUE_*`` / ``RESOURCE_UNRESCUE_*`` members in
:class:`waldur_core.logging.enums.EventType`. The handler builds the
event-type string from the executor's ``action`` and feeds it to
``EventType(...)``, which raises if the value is unknown.

These tests pin down the lookup so the next time an action executor is
introduced without an accompanying enum entry, CI catches it instead of
production.
"""

from unittest import mock

from django.test import SimpleTestCase
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.logging.enums import EVENT_GROUP_MAPPING, EventGroup, EventType
from waldur_openstack import executors, models
from waldur_openstack.handlers import _get_action_event_type

from . import factories, fixtures


class ActionEventTypeLookupTest(SimpleTestCase):
    """Unit tests for ``_get_action_event_type``.

    The helper takes an executor ``action`` label (e.g. ``"Rescue"``) and an
    event state (``"scheduled" | "succeeded" | "failed"``) and resolves it
    to an :class:`EventType` member. Missing members raise ``ValueError``,
    which propagates as a 500.
    """

    def test_rescue_action_resolves_for_every_state(self):
        self.assertEqual(
            _get_action_event_type("Rescue", "scheduled"),
            EventType.RESOURCE_RESCUE_SCHEDULED,
        )
        self.assertEqual(
            _get_action_event_type("Rescue", "succeeded"),
            EventType.RESOURCE_RESCUE_SUCCEEDED,
        )
        self.assertEqual(
            _get_action_event_type("Rescue", "failed"),
            EventType.RESOURCE_RESCUE_FAILED,
        )

    def test_unrescue_action_resolves_for_every_state(self):
        self.assertEqual(
            _get_action_event_type("Unrescue", "scheduled"),
            EventType.RESOURCE_UNRESCUE_SCHEDULED,
        )
        self.assertEqual(
            _get_action_event_type("Unrescue", "succeeded"),
            EventType.RESOURCE_UNRESCUE_SUCCEEDED,
        )
        self.assertEqual(
            _get_action_event_type("Unrescue", "failed"),
            EventType.RESOURCE_UNRESCUE_FAILED,
        )

    def test_executor_action_labels_resolve(self):
        """Each instance-level executor's ``.action`` must round-trip.

        The handler is wired to ``post_save`` on ``Instance``, ``Volume`` and
        ``Snapshot``; any ``ActionExecutor`` whose ``pre_apply`` flips one of
        those models into ``UPDATE_SCHEDULED`` triggers the lookup. The
        rescue/unrescue executors are the ones the production traceback
        exposed; including them explicitly here makes the contract obvious
        to readers.
        """
        for executor_cls in (
            executors.InstanceRescueExecutor,
            executors.InstanceUnrescueExecutor,
            executors.InstanceUpdateMetadataExecutor,
        ):
            for state in ("scheduled", "succeeded", "failed"):
                with self.subTest(executor=executor_cls.__name__, state=state):
                    # Must not raise.
                    _get_action_event_type(executor_cls.action, state)


class ResourceEventGroupMappingTest(SimpleTestCase):
    """The new event types must be advertised under the RESOURCES chip.

    ``EVENT_GROUP_MAPPING[EventGroup.RESOURCES]`` is the list the frontend
    uses to filter ``/api/events/?feature=resources``. Without this, the
    rescue events would emit but never surface in the audit log UI.
    """

    def test_rescue_event_types_listed(self):
        types = EVENT_GROUP_MAPPING[EventGroup.RESOURCES]
        self.assertIn(EventType.RESOURCE_RESCUE_SCHEDULED, types)
        self.assertIn(EventType.RESOURCE_RESCUE_SUCCEEDED, types)
        self.assertIn(EventType.RESOURCE_RESCUE_FAILED, types)

    def test_unrescue_event_types_listed(self):
        types = EVENT_GROUP_MAPPING[EventGroup.RESOURCES]
        self.assertIn(EventType.RESOURCE_UNRESCUE_SCHEDULED, types)
        self.assertIn(EventType.RESOURCE_UNRESCUE_SUCCEEDED, types)
        self.assertIn(EventType.RESOURCE_UNRESCUE_FAILED, types)

    def test_update_metadata_event_types_listed(self):
        types = EVENT_GROUP_MAPPING[EventGroup.RESOURCES]
        self.assertIn(EventType.RESOURCE_UPDATE_METADATA_SCHEDULED, types)
        self.assertIn(EventType.RESOURCE_UPDATE_METADATA_SUCCEEDED, types)
        self.assertIn(EventType.RESOURCE_UPDATE_METADATA_FAILED, types)


class RescueEndpointDoesNotCrashOnEventLogTest(test.APITestCase):
    """End-to-end regression for the production 500.

    The pre-existing rescue endpoint tests mock
    ``InstanceRescueExecutor.execute`` outright, which sidesteps
    ``pre_apply`` and therefore never triggers ``post_save`` ⇒
    ``log_action`` ⇒ ``EventType(...)``. We instead mock only the celery
    boundary so the full executor pipeline (including the signal handler
    that raised the ``ValueError``) runs against the real database.
    """

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        self.instance.state = CoreStates.OK
        self.instance.runtime_state = models.Instance.RuntimeStates.ACTIVE
        self.instance.save()
        # Drop the auto-created bootable volume so rescue without an
        # explicit image is allowed (matches the production caller's
        # image-backed instance).
        self.instance.volumes.update(bootable=False)

        # Stop transaction.on_commit from firing the celery dispatch — we
        # do NOT want to mock execute()/pre_apply() because the bug lives
        # in pre_apply ⇒ instance.save() ⇒ post_save ⇒ log_action.
        self.on_commit = mock.patch(
            "waldur_core.core.executors.transaction.on_commit",
            lambda func: None,
        )
        self.on_commit.start()
        self.addCleanup(self.on_commit.stop)

        self.client.force_authenticate(user=self.fixture.admin)

    def _url(self, action):
        return factories.InstanceFactory.get_url(self.instance, action=action)

    def test_rescue_endpoint_returns_202(self):
        response = self.client.post(self._url("rescue"), data={})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.action, "Rescue")
        self.assertEqual(self.instance.state, CoreStates.UPDATE_SCHEDULED)

    def test_unrescue_endpoint_returns_202(self):
        # Drive the instance into the rescue runtime state so the unrescue
        # validator (RuntimeStateValidator("RESCUE")) passes.
        self.instance.runtime_state = "RESCUE"
        self.instance.save()
        response = self.client.post(self._url("unrescue"), data={})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.action, "Unrescue")
        self.assertEqual(self.instance.state, CoreStates.UPDATE_SCHEDULED)
