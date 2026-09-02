"""Backend paths that need neither a vCenter nor the simulator.

vcsim's inventory has only standard and distributed port groups, and it will
not produce two concurrent device attachments, so the NSX and concurrency
branches are exercised here with constructed pyVmomi objects instead.
"""

from types import SimpleNamespace
from unittest import mock

from rest_framework import test

from waldur_vmware import backend, models, vim_utils

from . import factories


def make_backend(**options):
    settings = factories.VMwareServiceSettingsFactory(options=options)
    return backend.VMwareBackend(settings)


class VerifySslOptionTest(test.APITestCase):
    def test_missing_option_defaults_to_off(self):
        self.assertFalse(make_backend().verify_ssl)

    def test_boolean_option_is_honoured(self):
        self.assertTrue(make_backend(verify_ssl=True).verify_ssl)
        self.assertFalse(make_backend(verify_ssl=False).verify_ssl)

    def test_string_false_does_not_turn_verification_on(self):
        """`options` is a plain DictField, so it can hold the string "false".

        Every non-empty string is truthy, so reading the option as-is would
        silently enable verification for a deployment that asked for it off.
        """
        for value in ("false", "False", "no", "0", "off", ""):
            with self.subTest(value=value):
                self.assertFalse(make_backend(verify_ssl=value).verify_ssl)

    def test_string_true_turns_verification_on(self):
        for value in ("true", "True", "yes", "1", "on"):
            with self.subTest(value=value):
                self.assertTrue(make_backend(verify_ssl=value).verify_ssl)


class DatastoreConversionTest(test.APITestCase):
    def test_datastore_without_a_reported_type_is_stored_as_empty(self):
        """Datastore.type is not nullable.

        One datastore vCenter does not report a type for would otherwise abort
        the whole pull with a NotNullViolation.
        """
        datastore = make_backend()._backend_datastore_to_datastore(
            {
                "moid": "datastore-1",
                "name": "ds",
                "summary.capacity": 1024 * 1024 * 1024,
                "summary.freeSpace": 1024 * 1024,
            }
        )
        self.assertEqual(datastore.type, "")


class ToolsInstallTypeTest(test.APITestCase):
    def test_absent_install_type_does_not_mean_tools_are_installed(self):
        self.assertFalse(vim_utils.map_tools_installed(None))
        self.assertFalse(vim_utils.map_tools_installed(""))

    def test_unknown_install_type_means_tools_are_not_installed(self):
        self.assertFalse(vim_utils.map_tools_installed("guestToolsTypeUnknown"))

    def test_a_reported_install_type_means_tools_are_installed(self):
        self.assertTrue(vim_utils.map_tools_installed("guestToolsTypeMSI"))
        self.assertTrue(vim_utils.map_tools_installed("guestToolsTypeOpenVMTools"))


class OpaqueNetworkTest(test.APITestCase):
    """NSX-backed networks are pulled by pull_networks, so they are selectable.

    They need a backing of their own — vCenter rejects one supplied as a plain
    network backing — and their NIC backing carries the NSX id rather than a
    managed object reference, so a pulled port has to be translated back to the
    MoID stored as Network.backend_id.
    """

    def setUp(self):
        self.backend = make_backend()
        self.network = models.Network(
            settings=self.backend.settings,
            backend_id="network-o1",
            name="nsx-segment",
            type=vim_utils.NETWORK_TYPE_OPAQUE,
        )

    def test_opaque_network_gets_an_opaque_backing(self):
        from pyVmomi import vim

        summary = SimpleNamespace(
            opaqueNetworkId="nsx-1", opaqueNetworkType="nsx.LogicalSwitch"
        )
        self.backend._get_opaque_network_summary = mock.Mock(return_value=summary)

        result = self.backend._get_network_backing(self.network)

        self.assertIsInstance(
            result,
            vim.vm.device.VirtualEthernetCard.OpaqueNetworkBackingInfo,
        )
        self.assertEqual(result.opaqueNetworkId, "nsx-1")
        self.assertEqual(result.opaqueNetworkType, "nsx.LogicalSwitch")

    def test_opaque_backing_resolves_to_the_stored_network_id(self):
        """OpaqueNetworkBackingInfo has no `network` property at all.

        Reading one with getattr returns None, and pull_vm_ports then skips the
        adapter reporting that its network is unknown — which it is not.
        """
        from pyVmomi import vim

        self.backend.__dict__["_opaque_network_ids"] = {"nsx-1": "network-o1"}
        card = vim.vm.device.VirtualVmxnet3(
            backing=vim.vm.device.VirtualEthernetCard.OpaqueNetworkBackingInfo(
                opaqueNetworkId="nsx-1", opaqueNetworkType="nsx.LogicalSwitch"
            )
        )

        self.assertEqual(self.backend._get_port_network_id(card), "network-o1")


class AddDeviceTest(test.APITestCase):
    """ReconfigVM_Task reports no result, so the new device has to be found.

    Diffing device keys around the call is not atomic: a second attach on the
    same VM in between lands in the diff too, and both callers then claim the
    same key. That produced two Port rows sharing a backend_id, one describing
    an adapter it was not attached to, with nothing raising.
    """

    def setUp(self):
        self.backend = make_backend()
        self.backend._reconfigure_vm = mock.Mock()

    def make_nic(self, key, device_name):
        from pyVmomi import vim

        backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
            deviceName=device_name
        )
        # `key` is not optional on a vim device, and passing None explicitly is
        # not the same as leaving it unset — which is what a device being added
        # does, since vCenter assigns the key.
        if key is None:
            return vim.vm.device.VirtualVmxnet3(backing=backing)
        return vim.vm.device.VirtualVmxnet3(key=key, backing=backing)

    def attach(self, device_name, devices_after, matches=None):
        self.backend._get_vm_devices = mock.Mock(side_effect=[[], devices_after])
        return self.backend._add_device(
            "vm-1", self.make_nic(None, device_name), "Creating", matches=matches
        )

    def test_single_attachment_returns_the_new_key(self):
        self.assertEqual(
            self.attach("VM Network", [self.make_nic(203, "VM Network")]), "203"
        )

    def test_concurrent_attachment_is_resolved_by_the_requested_backing(self):
        requested = self.make_nic(None, "VM Network")
        devices_after = [
            self.make_nic(203, "Other Network"),
            self.make_nic(204, "VM Network"),
        ]

        key = self.attach(
            "VM Network",
            devices_after,
            matches=lambda device: self.backend._backing_identity(device.backing)
            == self.backend._backing_identity(requested.backing),
        )

        self.assertEqual(key, "204")

    def test_an_ambiguity_that_survives_is_an_error(self):
        """Two adapters on the same network cannot be told apart.

        Returning either would hand the caller a key that may belong to another
        request, so this fails instead.
        """
        requested = self.make_nic(None, "VM Network")
        devices_after = [
            self.make_nic(203, "VM Network"),
            self.make_nic(204, "VM Network"),
        ]

        with self.assertRaises(backend.VMwareBackendError):
            self.attach(
                "VM Network",
                devices_after,
                matches=lambda device: self.backend._backing_identity(device.backing)
                == self.backend._backing_identity(requested.backing),
            )

    def test_no_new_device_is_an_error(self):
        with self.assertRaises(backend.VMwareBackendError):
            self.attach("VM Network", [])


class DiskPlacementTest(test.APITestCase):
    """Unit numbers are shared by every device on a controller, not just disks."""

    def setUp(self):
        self.backend = make_backend()

    def controller(self, key, unit_numbers_taken_by_disks=(), reserved=7):
        from pyVmomi import vim

        controller = vim.vm.device.VirtualLsiLogicSASController(
            key=key, busNumber=0, scsiCtlrUnitNumber=reserved
        )
        disks = [
            vim.vm.device.VirtualDisk(
                key=2000 + unit, controllerKey=key, unitNumber=unit
            )
            for unit in unit_numbers_taken_by_disks
        ]
        return controller, disks

    def test_a_non_disk_device_holds_its_unit_number(self):
        from pyVmomi import vim

        controller, disks = self.controller(1000, [0])
        cdrom = vim.vm.device.VirtualCdrom(key=3000, controllerKey=1000, unitNumber=1)
        self.backend._get_vm_devices = mock.Mock(
            return_value=[controller, *disks, cdrom]
        )

        _, unit_number = self.backend._get_disk_placement("vm-1")

        self.assertEqual(unit_number, 2)

    def test_the_controller_own_unit_number_is_not_offered(self):
        controller, disks = self.controller(1000, range(7), reserved=7)
        self.backend._get_vm_devices = mock.Mock(return_value=[controller, *disks])

        _, unit_number = self.backend._get_disk_placement("vm-1")

        self.assertEqual(unit_number, 8)

    def test_a_full_controller_falls_through_to_the_next_one(self):
        first, first_disks = self.controller(1000, [n for n in range(16) if n != 7])
        second, second_disks = self.controller(1001, [0])
        self.backend._get_vm_devices = mock.Mock(
            return_value=[first, *first_disks, second, *second_disks]
        )

        controller, unit_number = self.backend._get_disk_placement("vm-1")

        self.assertEqual(controller.key, 1001)
        self.assertEqual(unit_number, 1)

    def test_a_vm_without_a_controller_is_an_error(self):
        self.backend._get_vm_devices = mock.Mock(return_value=[])

        with self.assertRaises(backend.VMwareBackendError):
            self.backend._get_disk_placement("vm-1")
