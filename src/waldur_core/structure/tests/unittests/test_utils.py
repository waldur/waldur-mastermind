import datetime
import unittest
from unittest import mock

from django.utils import timezone

from waldur_core.core.enums import CoreStates
from waldur_core.structure.utils import (
    handle_resource_not_found,
    handle_resource_update_success,
    update_pulled_fields,
)


class InstanceMock:
    def __init__(
        self,
        name="Virtual machine",
        runtime_state="OK",
        error_message="",
        directly_connected_ips="",
    ):
        self.pk = 1
        self.name = name
        self.runtime_state = runtime_state
        self.error_message = error_message
        self.directly_connected_ips = directly_connected_ips
        self.save = mock.Mock()


class UpdatePulledFieldsTest(unittest.TestCase):
    def test_model_is_not_saved_if_pulled_fields_are_the_same(self):
        vm = InstanceMock()
        update_pulled_fields(vm, vm, ("name", "runtime_state"))
        self.assertEqual(vm.save.call_count, 0)

    def test_model_is_saved_if_pulled_fields_are_different(self):
        vm1 = InstanceMock()
        vm2 = InstanceMock(runtime_state="ERRED")
        update_pulled_fields(vm1, vm2, ("name", "runtime_state"))
        self.assertEqual(vm1.save.call_count, 1)

    def test_model_is_not_saved_if_changed_fields_are_ignored(self):
        vm1 = InstanceMock()
        vm2 = InstanceMock(runtime_state="ERRED")
        update_pulled_fields(vm1, vm2, ("name",))
        self.assertEqual(vm1.save.call_count, 0)

    def test_error_message_saved_if_it_changed(self):
        vm1 = InstanceMock()
        vm2 = InstanceMock(error_message="Server does not respond.")
        update_pulled_fields(vm1, vm2, ("name",))
        self.assertEqual(vm1.save.call_count, 1)

    def test_comma_separated_strings_field(self):
        vm1 = InstanceMock(
            directly_connected_ips="192.168.42.69,171.22.247.92,172.16.202.63"
        )
        vm2 = InstanceMock(
            directly_connected_ips="172.16.202.63,192.168.42.69,171.22.247.92"
        )
        update_pulled_fields(vm1, vm2, ("directly_connected_ips",))
        self.assertEqual(vm1.save.call_count, 0)

        vm1 = InstanceMock(
            directly_connected_ips="292.168.42.69,171.22.247.92,172.16.202.63"
        )
        vm2 = InstanceMock(
            directly_connected_ips="172.16.202.63,192.168.42.69,171.22.247.92"
        )
        update_pulled_fields(vm1, vm2, ("directly_connected_ips",))
        self.assertEqual(vm1.save.call_count, 1)


class ResourceMock:
    """Mock resource for handle_resource_update_success tests."""

    def __init__(
        self,
        state=CoreStates.OK,
        error_message="",
        task_id=None,
        backend_missing_since=None,
    ):
        self.pk = 1
        self.state = state
        self.error_message = error_message
        self.task_id = task_id
        self.backend_missing_since = backend_missing_since
        self.runtime_state = "OK"
        self.save = mock.Mock()

    def recover(self):
        self.state = CoreStates.OK

    def set_ok(self):
        self.state = CoreStates.OK

    def set_erred(self):
        self.state = CoreStates.ERRED


class HandleResourceUpdateSuccessTest(unittest.TestCase):
    """Tests for handle_resource_update_success function.

    Fixes PUHURI-PORTALS-DYH (N+1 query issue).
    """

    def test_save_not_called_when_no_changes_needed(self):
        """Test that save is not called when resource is OK with no task_id."""
        resource = ResourceMock(state=CoreStates.OK, error_message="", task_id=None)
        handle_resource_update_success(resource)
        self.assertEqual(resource.save.call_count, 0)

    def test_save_called_when_task_id_needs_clearing(self):
        """Test that save is called when task_id needs to be cleared."""
        resource = ResourceMock(
            state=CoreStates.OK, error_message="", task_id="some-task-id"
        )
        handle_resource_update_success(resource)
        self.assertEqual(resource.save.call_count, 1)
        self.assertIsNone(resource.task_id)
        # Verify update_fields contains task_id
        resource.save.assert_called_once()
        call_kwargs = resource.save.call_args[1]
        self.assertIn("task_id", call_kwargs["update_fields"])

    def test_save_called_when_error_message_needs_clearing(self):
        """Test that save is called when error_message needs to be cleared."""
        resource = ResourceMock(
            state=CoreStates.OK, error_message="Some error", task_id=None
        )
        handle_resource_update_success(resource)
        self.assertEqual(resource.save.call_count, 1)
        self.assertEqual(resource.error_message, "")

    def test_save_called_when_state_erred(self):
        """Test that save is called when state is ERRED."""
        resource = ResourceMock(state=CoreStates.ERRED, error_message="", task_id=None)
        handle_resource_update_success(resource)
        self.assertEqual(resource.save.call_count, 1)
        self.assertEqual(resource.state, CoreStates.OK)

    def test_multiple_fields_updated_in_single_save(self):
        """Test that multiple fields are updated in a single save call."""
        resource = ResourceMock(
            state=CoreStates.ERRED, error_message="Error", task_id="task-123"
        )
        handle_resource_update_success(resource)
        # Should only call save once, not multiple times
        self.assertEqual(resource.save.call_count, 1)
        self.assertEqual(resource.state, CoreStates.OK)
        self.assertEqual(resource.error_message, "")
        self.assertIsNone(resource.task_id)


class HandleResourceNotFoundTest(unittest.TestCase):
    message = "Does not exist at backend."

    def test_resource_is_marked_as_erred_and_reported(self):
        resource = ResourceMock(state=CoreStates.OK)
        with self.assertLogs("waldur_core.structure.utils", "WARNING"):
            handle_resource_not_found(resource)
        self.assertEqual(resource.state, CoreStates.ERRED)
        self.assertIn(self.message, resource.error_message)
        self.assertIsNotNone(resource.backend_missing_since)

    def test_already_missing_resource_is_reported_at_debug_level(self):
        resource = ResourceMock(
            state=CoreStates.ERRED,
            error_message=self.message,
            backend_missing_since=timezone.now() - datetime.timedelta(days=10),
        )
        with self.assertLogs("waldur_core.structure.utils", "DEBUG") as logs:
            handle_resource_not_found(resource)
        self.assertEqual([record.levelname for record in logs.records], ["DEBUG"])

    def test_already_missing_resource_is_not_saved_again(self):
        resource = ResourceMock(
            state=CoreStates.ERRED,
            error_message=self.message,
            backend_missing_since=timezone.now() - datetime.timedelta(days=10),
        )
        resource.runtime_state = ""
        handle_resource_not_found(resource)
        self.assertEqual(resource.save.call_count, 0)

    def test_missing_since_is_not_overwritten_on_subsequent_pulls(self):
        missing_since = timezone.now() - datetime.timedelta(days=10)
        resource = ResourceMock(
            state=CoreStates.ERRED,
            error_message=self.message,
            backend_missing_since=missing_since,
        )
        handle_resource_not_found(resource)
        self.assertEqual(resource.backend_missing_since, missing_since)
        self.assertEqual(resource.runtime_state, "")

    def test_resource_erred_for_another_reason_is_reported_as_missing(self):
        resource = ResourceMock(
            state=CoreStates.ERRED, error_message="Failed to provision."
        )
        with self.assertLogs("waldur_core.structure.utils", "WARNING"):
            handle_resource_not_found(resource)
        self.assertIn(self.message, resource.error_message)
        self.assertIsNotNone(resource.backend_missing_since)

    def test_missing_since_is_cleared_once_resource_is_seen_again(self):
        resource = ResourceMock(
            state=CoreStates.ERRED,
            error_message=self.message,
            backend_missing_since=timezone.now() - datetime.timedelta(days=10),
        )
        handle_resource_update_success(resource)
        self.assertIsNone(resource.backend_missing_since)
        call_kwargs = resource.save.call_args[1]
        self.assertIn("backend_missing_since", call_kwargs["update_fields"])
