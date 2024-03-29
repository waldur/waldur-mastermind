import unittest
from unittest import mock

from waldur_core.structure.utils import update_pulled_fields


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
