"""Pull paths exercised against a real vCenter simulator.

These replace the MagicMock-based pull tests: the old ones asserted that the
backend passed REST dictionaries around, which proved nothing about the wire.
"""

import gc

import pytest

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_vmware import models, vim_utils
from waldur_vmware.backend import VMwareBackend, VMwareBackendError

from . import factories
from .vcsim import VcsimTestCase

pytestmark = pytest.mark.vcsim


class PingTest(VcsimTestCase):
    def test_ping_succeeds_against_a_reachable_vcenter(self):
        self.assertTrue(self.backend.ping())

    def test_ping_fails_when_vcenter_is_unreachable(self):
        self.service_settings.backend_url = "https://127.0.0.1:1"
        self.service_settings.save()
        self.assertFalse(self.backend.ping())


class ClusterPullTest(VcsimTestCase):
    def test_clusters_are_pulled_from_the_inventory(self):
        self.backend.pull_clusters()

        clusters = models.Cluster.objects.filter(settings=self.service_settings)
        self.assertTrue(clusters.exists())
        # vim25 identifiers are what the REST API used to return, so backend_id
        # keeps its meaning across the migration.
        for cluster in clusters:
            self.assertTrue(cluster.backend_id.startswith("domain-c"))
            self.assertTrue(cluster.name)

    def test_clusters_missing_from_the_inventory_are_deleted(self):
        factories.ClusterFactory(
            settings=self.service_settings, backend_id="domain-c999"
        )

        self.backend.pull_clusters()

        self.assertFalse(
            models.Cluster.objects.filter(
                settings=self.service_settings, backend_id="domain-c999"
            ).exists()
        )

    def test_renamed_cluster_is_updated_in_place(self):
        self.backend.pull_clusters()
        cluster = models.Cluster.objects.filter(settings=self.service_settings).first()
        cluster.name = "stale name"
        cluster.save()

        self.backend.pull_clusters()

        cluster.refresh_from_db()
        self.assertNotEqual(cluster.name, "stale name")

    def test_repeated_pull_does_not_duplicate_clusters(self):
        self.backend.pull_clusters()
        count = models.Cluster.objects.filter(settings=self.service_settings).count()

        self.backend.pull_clusters()

        self.assertEqual(
            models.Cluster.objects.filter(settings=self.service_settings).count(), count
        )


class NetworkPullTest(VcsimTestCase):
    def test_networks_are_pulled_with_their_type(self):
        self.backend.pull_networks()

        networks = models.Network.objects.filter(settings=self.service_settings)
        self.assertTrue(networks.exists())
        types = set(networks.values_list("type", flat=True))
        self.assertIn(vim_utils.NETWORK_TYPE_STANDARD, types)

    def test_distributed_port_groups_are_classified_separately(self):
        self.backend.pull_networks()

        distributed = models.Network.objects.filter(
            settings=self.service_settings, type=vim_utils.NETWORK_TYPE_DISTRIBUTED
        )
        # vcsim's stock inventory includes a distributed switch, so this asserts
        # the classification rather than merely that the code path exists.
        self.assertTrue(distributed.exists())
        for network in distributed:
            self.assertTrue(network.backend_id.startswith("dvportgroup-"))

    def test_networks_missing_from_the_inventory_are_deleted(self):
        factories.NetworkFactory(
            settings=self.service_settings, backend_id="network-999"
        )

        self.backend.pull_networks()

        self.assertFalse(
            models.Network.objects.filter(
                settings=self.service_settings, backend_id="network-999"
            ).exists()
        )


class DatastorePullTest(VcsimTestCase):
    def test_datastores_are_pulled_with_capacity(self):
        self.backend.pull_datastores()

        datastores = models.Datastore.objects.filter(settings=self.service_settings)
        self.assertTrue(datastores.exists())
        for datastore in datastores:
            self.assertTrue(datastore.backend_id.startswith("datastore-"))
            self.assertGreater(datastore.capacity, 0)
            self.assertIsNotNone(datastore.type)

    def test_datastores_missing_from_the_inventory_are_deleted(self):
        factories.DatastoreFactory(
            settings=self.service_settings, backend_id="datastore-999"
        )

        self.backend.pull_datastores()

        self.assertFalse(
            models.Datastore.objects.filter(
                settings=self.service_settings, backend_id="datastore-999"
            ).exists()
        )


class FolderPullTest(VcsimTestCase):
    def test_only_virtual_machine_folders_are_pulled(self):
        self.backend.pull_folders()

        folders = models.Folder.objects.filter(settings=self.service_settings)
        self.assertTrue(folders.exists())

        # vCenter keeps a separate folder tree per entity kind; the host,
        # datastore and network roots must not show up as VM folders.
        pulled_ids = set(folders.values_list("backend_id", flat=True))
        vm_folder_ids = {folder["moid"] for folder in self.backend.get_vm_folders()}
        self.assertEqual(pulled_ids, vm_folder_ids)

    def test_folders_missing_from_the_inventory_are_deleted(self):
        factories.FolderFactory(settings=self.service_settings, backend_id="group-v999")

        self.backend.pull_folders()

        self.assertFalse(
            models.Folder.objects.filter(
                settings=self.service_settings, backend_id="group-v999"
            ).exists()
        )


class DefaultPlacementTest(VcsimTestCase):
    def test_default_folder_is_a_virtual_machine_folder(self):
        default = self.backend.get_default_vm_folder()
        self.assertIn(
            default, {folder["moid"] for folder in self.backend.get_vm_folders()}
        )

    def test_default_resource_pool_is_a_resource_pool(self):
        self.assertTrue(
            self.backend.get_default_resource_pool().startswith("resgroup-")
        )

    def test_default_datastore_is_a_datastore(self):
        self.assertTrue(self.backend.get_default_datastore().startswith("datastore-"))


class TransportErrorTest(VcsimTestCase):
    def test_unreachable_vcenter_is_reported_as_a_backend_error(self):
        """A socket error has to reach callers as ServiceBackendError.

        BackgroundPullTask only catches ServiceBackendError; anything else
        crashes the task instead of marking the resource ERRED with a message.
        """
        self.service_settings.backend_url = "https://127.0.0.1:1"
        self.service_settings.save()

        with self.assertRaises(VMwareBackendError):
            self.backend.pull_clusters()


class PortPullTest(VcsimTestCase):
    def test_port_on_an_unknown_network_is_skipped(self):
        """Port.network is not nullable, so an unknown network must not be saved.

        A NIC can sit on a network added since the last pull_networks; writing
        None into the FK would raise IntegrityError instead.
        """
        project = structure_fixtures.ProjectFixture().project
        self.backend.pull_folders()
        self.backend.pull_clusters()
        self.backend.pull_datastores()
        # Deliberately do NOT pull networks, so every NIC's network is unknown.

        vm_backend_id = self._first_stock_vm()
        vm = factories.VirtualMachineFactory(
            service_settings=self.service_settings,
            project=project,
            backend_id=vm_backend_id,
        )

        self.backend.pull_vm_ports(vm)

        self.assertFalse(models.Port.objects.filter(vm=vm).exists())


class UnansweredPropertyTest(VcsimTestCase):
    """vCenter can decline to answer for a property without saying so.

    `guest.guestState` comes back in neither propSet nor missingSet on a stock
    vcsim VM, so the helper simply omits it. A caller that indexes the result
    would get a bare KeyError from inside a pull task, where only
    ServiceBackendError is caught — the task crashes instead of marking the
    resource ERRED.
    """

    def test_a_required_property_that_is_not_answered_is_a_backend_error(self):
        backend_id = self._first_stock_vm()

        with self.assertRaises(VMwareBackendError):
            self.backend._get_vm_properties(
                backend_id, ["guest.guestState"], required=["guest.guestState"]
            )

    def test_a_required_property_that_is_answered_is_returned(self):
        backend_id = self._first_stock_vm()

        properties = self.backend._get_vm_properties(
            backend_id, ["name"], required=["name"]
        )

        self.assertTrue(properties["name"])

    def test_tools_are_not_installed_when_the_install_type_is_not_reported(self):
        """An absent property is not evidence that VMware Tools are present.

        Reading it as such offers guest shutdown and reboot on a VM that can
        perform neither.
        """
        backend_id = self._first_stock_vm()

        self.assertFalse(self.backend.get_vm_tools_installed(backend_id))


class SessionLifecycleTest(VcsimTestCase):
    """A vCenter session outlives the backend that opened it.

    A backend is constructed fresh for every Celery task and nothing in the
    structure app disposes of one, so before this the session stayed open until
    vCenter idled it out — one per service settings object, on every pull.
    """

    def observed_session_keys(self):
        """Session keys as a second, independent connection sees them."""
        observer = self.observer.soap_client.content.sessionManager
        return {session.key for session in observer.sessionList or []}

    def setUp(self):
        super().setUp()
        self.observer = VMwareBackend(self.service_settings)
        self.addCleanup(self.observer.close)

    def test_closing_the_backend_ends_the_vcenter_session(self):
        session_key = self.backend.soap_client.content.sessionManager.currentSession.key
        self.assertIn(session_key, self.observed_session_keys())

        self.backend.close()

        self.assertNotIn(session_key, self.observed_session_keys())

    def test_collecting_the_backend_ends_the_vcenter_session(self):
        """close() is not on any current call path, so collection has to do it.

        Nothing disposes of a backend explicitly today; the finalizer is what
        makes the session's lifetime match the object's.
        """
        backend = VMwareBackend(self.service_settings)
        session_key = backend.soap_client.content.sessionManager.currentSession.key
        self.assertIn(session_key, self.observed_session_keys())

        del backend
        gc.collect()

        self.assertNotIn(session_key, self.observed_session_keys())

    def test_closing_a_backend_that_never_connected_is_harmless(self):
        VMwareBackend(self.service_settings).close()

    def test_closing_twice_is_harmless(self):
        self.backend.ping()
        self.backend.close()
        self.backend.close()


class VirtualMachineImportTest(VcsimTestCase):
    def test_stock_virtual_machine_is_imported(self):
        backend_id = self._first_stock_vm()

        vm = self.backend.import_virtual_machine(backend_id, save=False)

        self.assertEqual(vm.backend_id, backend_id)
        self.assertTrue(vm.name)
        # The database stores the REST spelling; vim25 says `poweredOn`.
        self.assertIn(
            vm.runtime_state,
            {
                models.VirtualMachine.RuntimeStates.POWERED_ON,
                models.VirtualMachine.RuntimeStates.POWERED_OFF,
                models.VirtualMachine.RuntimeStates.SUSPENDED,
            },
        )
        self.assertGreaterEqual(vm.cores, 1)
        self.assertGreaterEqual(vm.ram, 1)
