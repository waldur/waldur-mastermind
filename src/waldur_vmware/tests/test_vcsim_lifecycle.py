"""The full order path, end to end against the simulator.

Deploy from template -> poll until powered on -> reconfigure CPU/RAM -> add NIC
-> add disk -> destroy. This is the acceptance scenario for the pyVmomi
migration, and the one thing the MagicMock suite could never express.
"""

import pytest

from waldur_core.core.enums import CoreStates
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_vmware import models, vim_utils

from . import factories
from .vcsim import VcsimTestCase

pytestmark = pytest.mark.vcsim


class VirtualMachineLifecycleTest(VcsimTestCase):
    def setUp(self):
        super().setUp()
        self.project = structure_fixtures.ProjectFixture().project

        # Seed the local database from the simulator's real inventory, so that
        # placement uses identifiers vCenter will actually accept.
        self.backend.pull_folders()
        self.backend.pull_clusters()
        self.backend.pull_networks()
        self.backend.pull_datastores()

        self.cluster = models.Cluster.objects.filter(
            settings=self.service_settings
        ).first()
        self.datastore = models.Datastore.objects.filter(
            settings=self.service_settings
        ).first()
        self.folder = models.Folder.objects.filter(
            settings=self.service_settings
        ).first()
        self.network = models.Network.objects.filter(
            settings=self.service_settings,
            type="STANDARD_PORTGROUP",
        ).first()

    def _create_vm(self, template=None, cluster=None):
        vm = factories.VirtualMachineFactory(
            service_settings=self.service_settings,
            project=self.project,
            template=template,
            cluster=cluster,
            datastore=self.datastore,
            folder=self.folder,
            cores=1,
            cores_per_socket=1,
            ram=512,
            guest_os="UBUNTU_64",
        )
        vm.backend_id = ""
        vm.save()
        return vm

    def test_full_order_path_from_template(self):
        # Deployed without a cluster, so that _get_vm_placement() supplies a
        # resource pool. vcsim rejects a Content Library deployment whose
        # placement names only a cluster, while a live vCenter derives the
        # cluster's root resource pool itself — so the cluster-only branch is
        # one to confirm during acceptance testing.
        library_item_id = self.create_library_template()
        template = factories.TemplateFactory(
            settings=self.service_settings, backend_id=library_item_id
        )
        vm = self._create_vm(template=template)

        # --- deploy ---
        self.backend.create_virtual_machine(vm)
        vm.refresh_from_db()
        self.assertTrue(vm.backend_id.startswith("vm-"))
        self.addCleanup(self._destroy_quietly, vm)
        self.clear_template_flag(vm.backend_id)

        # --- power on, then observe the state vCenter reports ---
        self.backend.start_virtual_machine(vm)
        self.backend.pull_virtual_machine_runtime_state(vm)
        vm.refresh_from_db()
        self.assertEqual(
            vm.runtime_state, models.VirtualMachine.RuntimeStates.POWERED_ON
        )

        # --- reconfigure CPU and RAM ---
        # vcsim accepts hot-add; a live vCenter may require a powered-off VM for
        # some guest types, which is one of the things acceptance testing covers.
        vm.cores = 2
        vm.cores_per_socket = 2
        vm.ram = 1024
        vm.save()
        self.backend.update_virtual_machine(vm)

        imported = self.backend.import_virtual_machine(vm.backend_id, save=False)
        self.assertEqual(imported.cores, 2)
        self.assertEqual(imported.cores_per_socket, 2)
        self.assertEqual(imported.ram, 1024)

        # --- add a network adapter ---
        port = models.Port.objects.create(
            vm=vm,
            network=self.network,
            service_settings=self.service_settings,
            project=self.project,
            name="eth-test",
            state=CoreStates.CREATING,
        )
        self.backend.create_port(port)
        port.refresh_from_db()
        self.assertTrue(port.backend_id)

        self.backend.pull_vm_ports(vm)
        self.assertTrue(
            models.Port.objects.filter(vm=vm, backend_id=port.backend_id).exists()
        )

        # --- add a disk ---
        disk = models.Disk.objects.create(
            vm=vm,
            service_settings=self.service_settings,
            project=self.project,
            name="disk-test",
            size=1024,
            state=CoreStates.CREATING,
        )
        self.backend.create_disk(disk)
        disk.refresh_from_db()
        self.assertTrue(disk.backend_id)

        imported_disk = self.backend.import_disk(
            vm.backend_id, disk.backend_id, save=False
        )
        self.assertEqual(imported_disk.size, 1024)

        # --- extend the disk ---
        disk.size = 2048
        disk.save()
        self.backend.extend_disk(disk)
        imported_disk = self.backend.import_disk(
            vm.backend_id, disk.backend_id, save=False
        )
        self.assertEqual(imported_disk.size, 2048)

        # --- tear the hardware back down ---
        self.backend.delete_disk(disk, delete_vmdk=False)
        self.backend.delete_port(port)

        self.backend.pull_vm_ports(vm)
        self.assertFalse(
            models.Port.objects.filter(vm=vm, backend_id=port.backend_id).exists()
        )

        # --- destroy ---
        self.backend.stop_virtual_machine(vm)
        self.backend.delete_virtual_machine(vm)

        from pyVmomi import vim

        remaining = {
            item["moid"]
            for item in vim_utils.collect_properties(
                self.backend.soap_client, vim.VirtualMachine, ["name"]
            )
        }
        self.assertNotIn(vm.backend_id, remaining)

    def test_virtual_machine_is_created_from_scratch(self):
        vm = self._create_vm(template=None, cluster=self.cluster)

        self.backend.create_virtual_machine(vm)
        vm.refresh_from_db()
        self.addCleanup(self._destroy_quietly, vm)

        self.assertTrue(vm.backend_id.startswith("vm-"))
        imported = self.backend.import_virtual_machine(vm.backend_id, save=False)
        self.assertEqual(imported.cores, vm.cores)
        self.assertEqual(imported.ram, vm.ram)

    def test_nic_can_be_attached_to_a_distributed_port_group(self):
        """A distributed port group needs a port-connection backing.

        vCenter rejects one supplied as a plain network backing, and the choice
        cannot be made by isinstance on a synthesised managed object reference —
        that always reports the base `vim.Network` type.
        """
        distributed = models.Network.objects.filter(
            settings=self.service_settings,
            type=vim_utils.NETWORK_TYPE_DISTRIBUTED,
        ).first()
        self.assertIsNotNone(distributed, "vcsim should provide a distributed switch")

        vm = self._create_vm(template=None, cluster=self.cluster)
        self.backend.create_virtual_machine(vm)
        vm.refresh_from_db()
        self.addCleanup(self._destroy_quietly, vm)

        port = models.Port.objects.create(
            vm=vm,
            network=distributed,
            service_settings=self.service_settings,
            project=self.project,
            name="dvs-test",
            state=CoreStates.CREATING,
        )
        self.backend.create_port(port)
        port.refresh_from_db()
        self.assertTrue(port.backend_id)

        from pyVmomi import vim

        card = self.backend._get_backend_nic(vm.backend_id, port.backend_id)
        self.assertIsInstance(
            card.backing,
            vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo,
        )
        self.assertEqual(card.backing.port.portgroupKey, distributed.backend_id)

    def test_disk_can_be_attached_to_a_vm_created_from_scratch(self):
        """vCenter gives a new VM no disk controller, so provisioning has to.

        Without one, every disk order on a from-scratch VM fails on "no SCSI
        controller to attach a disk to".
        """
        vm = self._create_vm(template=None, cluster=self.cluster)
        self.backend.create_virtual_machine(vm)
        vm.refresh_from_db()
        self.addCleanup(self._destroy_quietly, vm)

        disk = models.Disk.objects.create(
            vm=vm,
            service_settings=self.service_settings,
            project=self.project,
            name="scratch-disk",
            size=1024,
            state=CoreStates.CREATING,
        )
        self.backend.create_disk(disk)
        disk.refresh_from_db()
        self.assertTrue(disk.backend_id)

        imported = self.backend.import_disk(vm.backend_id, disk.backend_id, save=False)
        self.assertEqual(imported.size, 1024)

    def test_console_url_keeps_a_non_default_port(self):
        """`host` drops the port for SmartConnect, but the console URL needs it."""
        vm = self._create_vm(template=None, cluster=self.cluster)
        self.backend.create_virtual_machine(vm)
        vm.refresh_from_db()
        self.addCleanup(self._destroy_quietly, vm)

        url = self.backend.get_console_url(vm)
        self.assertIn(self.backend.netloc, url)
        self.assertIn(":8989", url)

    def test_power_operations_report_the_state_back(self):
        vm = self._create_vm(template=None, cluster=self.cluster)
        self.backend.create_virtual_machine(vm)
        vm.refresh_from_db()
        self.addCleanup(self._destroy_quietly, vm)

        for operation, expected in [
            (
                self.backend.start_virtual_machine,
                models.VirtualMachine.RuntimeStates.POWERED_ON,
            ),
            (
                self.backend.suspend_virtual_machine,
                models.VirtualMachine.RuntimeStates.SUSPENDED,
            ),
            (
                self.backend.start_virtual_machine,
                models.VirtualMachine.RuntimeStates.POWERED_ON,
            ),
            (
                self.backend.stop_virtual_machine,
                models.VirtualMachine.RuntimeStates.POWERED_OFF,
            ),
        ]:
            operation(vm)
            self.backend.pull_virtual_machine_runtime_state(vm)
            vm.refresh_from_db()
            self.assertEqual(vm.runtime_state, expected)

    def _destroy_quietly(self, vm):
        """Best-effort cleanup: vcsim has no reset, so tests undo their own work."""
        if not vm.backend_id:
            return
        try:
            self.backend.stop_virtual_machine(vm)
        except Exception:
            pass
        try:
            self.backend.delete_virtual_machine(vm)
        except Exception:
            pass
