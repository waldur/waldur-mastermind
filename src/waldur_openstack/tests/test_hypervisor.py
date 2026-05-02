from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.tests import factories
from waldur_openstack.tests.fixtures import OpenStackFixture, mock_session


class HypervisorApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()
        self.hypervisor = factories.HypervisorFactory(settings=self.fixture.settings)

    def test_staff_can_list_hypervisors(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.HypervisorFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) >= 1)

    def test_owner_of_service_provider_can_list_hypervisors(self):
        # fixture.owner owns fixture.customer which owns fixture.settings
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.HypervisorFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [h["uuid"] for h in response.data]
        self.assertIn(self.hypervisor.uuid.hex, uuids)

    def test_unrelated_user_cannot_see_hypervisors(self):
        # A user with no role on the service provider's customer sees nothing
        unrelated_user = structure_factories.UserFactory()
        self.client.force_authenticate(unrelated_user)
        response = self.client.get(factories.HypervisorFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_owner_of_other_service_provider_cannot_see_hypervisors(self):
        # Owner of a different service provider must not see hypervisors of this one
        other_fixture = OpenStackFixture()
        other_hypervisor = factories.HypervisorFactory(settings=other_fixture.settings)

        self.client.force_authenticate(other_fixture.owner)
        response = self.client.get(factories.HypervisorFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [h["uuid"] for h in response.data]
        self.assertNotIn(self.hypervisor.uuid.hex, uuids)
        self.assertIn(other_hypervisor.uuid.hex, uuids)

    def test_project_admin_cannot_see_hypervisors(self):
        # Project-level roles do not grant access to provider infrastructure
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(factories.HypervisorFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_project_manager_cannot_see_hypervisors(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(factories.HypervisorFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_project_member_cannot_see_hypervisors(self):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.get(factories.HypervisorFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_staff_can_retrieve_hypervisor(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.HypervisorFactory.get_url(self.hypervisor))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], self.hypervisor.name)
        self.assertEqual(
            response.data["hypervisor_type"], self.hypervisor.hypervisor_type
        )
        self.assertEqual(response.data["vcpus"], self.hypervisor.vcpus)
        self.assertEqual(response.data["vcpus_used"], self.hypervisor.vcpus_used)
        self.assertEqual(response.data["memory_mb"], self.hypervisor.memory_mb)
        self.assertEqual(
            response.data["memory_mb_used"], self.hypervisor.memory_mb_used
        )
        self.assertEqual(response.data["local_gb"], self.hypervisor.local_gb)
        self.assertEqual(response.data["local_gb_used"], self.hypervisor.local_gb_used)
        self.assertEqual(response.data["running_vms"], self.hypervisor.running_vms)
        self.assertEqual(response.data["state"], self.hypervisor.state)
        self.assertEqual(response.data["status"], self.hypervisor.status)

    def test_filter_by_settings_uuid(self):
        other_settings = structure_factories.ServiceSettingsFactory(type="OpenStack")
        factories.HypervisorFactory(settings=other_settings)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            factories.HypervisorFactory.get_list_url(),
            {"settings_uuid": self.fixture.settings.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.hypervisor.uuid.hex)

    def test_filter_by_hypervisor_type(self):
        factories.HypervisorFactory(
            settings=self.fixture.settings, hypervisor_type="QEMU"
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            factories.HypervisorFactory.get_list_url(),
            {
                "settings_uuid": self.fixture.settings.uuid.hex,
                "hypervisor_type": "KVM",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [h["uuid"] for h in response.data]
        self.assertIn(self.hypervisor.uuid.hex, uuids)
        for h in response.data:
            self.assertEqual(h["hypervisor_type"], "KVM")

    def test_list_is_read_only(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.HypervisorFactory.get_list_url(), data={"name": "new"}
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_summary_returns_aggregated_values(self):
        factories.HypervisorFactory(
            settings=self.fixture.settings,
            vcpus=40,
            vcpus_used=8,
            memory_mb=131072,
            memory_mb_used=4096,
            local_gb=3000,
            local_gb_used=100,
            running_vms=5,
        )
        self.client.force_authenticate(self.fixture.staff)
        url = factories.HypervisorFactory.get_list_url() + "summary/"
        response = self.client.get(
            url, {"settings_uuid": self.fixture.settings.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # self.hypervisor (from setUp) + the new one above
        self.assertEqual(response.data["total_vcpus"], self.hypervisor.vcpus + 40)
        self.assertEqual(response.data["used_vcpus"], self.hypervisor.vcpus_used + 8)
        self.assertEqual(
            response.data["total_memory_mb"], self.hypervisor.memory_mb + 131072
        )
        self.assertEqual(
            response.data["used_memory_mb"], self.hypervisor.memory_mb_used + 4096
        )
        self.assertEqual(
            response.data["total_local_gb"], self.hypervisor.local_gb + 3000
        )
        self.assertEqual(
            response.data["used_local_gb"], self.hypervisor.local_gb_used + 100
        )
        self.assertEqual(
            response.data["total_running_vms"], self.hypervisor.running_vms + 5
        )

    def test_summary_returns_zeros_when_no_hypervisors(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.HypervisorFactory.get_list_url() + "summary/"
        # Use a settings_uuid that has no hypervisors
        empty_settings = structure_factories.ServiceSettingsFactory(type="OpenStack")
        response = self.client.get(url, {"settings_uuid": empty_settings.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_vcpus"], 0)
        self.assertEqual(response.data["used_vcpus"], 0)

    def test_summary_requires_settings_uuid(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.HypervisorFactory.get_list_url() + "summary/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_summary_does_not_include_other_provider_hypervisors(self):
        # summary must be isolated per service provider
        other_fixture = OpenStackFixture()
        factories.HypervisorFactory(settings=other_fixture.settings, vcpus=999)

        self.client.force_authenticate(self.fixture.owner)
        url = factories.HypervisorFactory.get_list_url() + "summary/"
        response = self.client.get(
            url, {"settings_uuid": self.fixture.settings.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_vcpus"], self.hypervisor.vcpus)


class PullHypervisorsTest(test.APITestCase):
    """pull_hypervisors now sources capacity from Placement (the legacy
    /os-hypervisors capacity fields are removed at Nova microversion 2.88, and
    the values were also documented as misleading regarding CPU pinning, shared
    storage, and overcommit). Host metadata (hostname, type, state, status,
    running_vms) still comes from Nova for the time being.
    """

    def setUp(self):
        self.fixture = OpenStackFixture()
        self.nova_patcher = mock.patch("waldur_openstack.backend.get_nova_client")
        self.placement_patcher = mock.patch(
            "waldur_openstack.backend.get_placement_client"
        )
        self.mock_nova = self.nova_patcher.start()
        self.mock_placement = self.placement_patcher.start()
        # Default: empty placement (resource provider list returns nothing).
        self.mock_placement.return_value.list_resource_providers.return_value = []
        mock_session()

    def tearDown(self):
        mock.patch.stopall()

    def _set_placement(self, hostname_to_capacity):
        """Convenience: set up Placement mock to return one resource provider
        per hostname with the given (vcpus, memory_mb, local_gb) capacity and
        usage tuples. Format:
        {
          "oscompute01": {
            "VCPU": (total, reserved, allocation_ratio, used),
            "MEMORY_MB": (...),
            "DISK_GB": (...),
          },
          ...
        }
        """
        rps = []
        inventories_by_uuid = {}
        usages_by_uuid = {}
        for idx, (hostname, classes) in enumerate(hostname_to_capacity.items()):
            uuid_str = f"rp-uuid-{idx}"
            rps.append({"uuid": uuid_str, "name": hostname})
            inventories_by_uuid[uuid_str] = {
                cls: {
                    "total": total,
                    "reserved": reserved,
                    "allocation_ratio": allocation_ratio,
                }
                for cls, (total, reserved, allocation_ratio, _) in classes.items()
            }
            usages_by_uuid[uuid_str] = {
                cls: used for cls, (_, _, _, used) in classes.items()
            }
        self.mock_placement.return_value.list_resource_providers.return_value = rps
        self.mock_placement.return_value.get_inventories.side_effect = (
            lambda u: inventories_by_uuid.get(u, {})
        )
        self.mock_placement.return_value.get_usages.side_effect = (
            lambda u: usages_by_uuid.get(u, {})
        )

    def _make_remote_hypervisor(self, id, hostname, hypervisor_type="KVM", **kwargs):
        h = mock.MagicMock()
        h.id = id
        h.hypervisor_hostname = hostname
        h.hypervisor_type = hypervisor_type
        h.running_vms = kwargs.get("running_vms", 3)
        h.state = kwargs.get("state", "up")
        h.status = kwargs.get("status", "enabled")
        return h

    def test_new_hypervisor_is_created_with_placement_capacity(self):
        remote = self._make_remote_hypervisor(1, "oscompute01", "KVM")
        self.mock_nova.return_value.hypervisors.list.return_value = [remote]
        self._set_placement(
            {
                "oscompute01": {
                    "VCPU": (40, 0, 1.0, 2),
                    "MEMORY_MB": (131072, 0, 1.0, 3072),
                    "DISK_GB": (3000, 0, 1.0, 11),
                }
            }
        )

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_hypervisors()

        hypervisor = models.Hypervisor.objects.get(
            settings=self.fixture.settings, backend_id="1"
        )
        self.assertEqual(hypervisor.name, "oscompute01")
        self.assertEqual(hypervisor.hypervisor_type, "KVM")
        self.assertEqual(hypervisor.vcpus, 40)
        self.assertEqual(hypervisor.vcpus_used, 2)
        self.assertEqual(hypervisor.memory_mb, 131072)
        self.assertEqual(hypervisor.memory_mb_used, 3072)
        self.assertEqual(hypervisor.local_gb, 3000)
        self.assertEqual(hypervisor.local_gb_used, 11)
        self.assertEqual(hypervisor.running_vms, 3)
        self.assertEqual(hypervisor.state, "up")
        self.assertEqual(hypervisor.status, "enabled")

    def test_allocation_ratio_is_honored(self):
        # Memory at 1.5x overcommit: 131072 total, 16384 reserved → effective
        # (131072 - 16384) * 1.5 = 172032
        remote = self._make_remote_hypervisor(1, "oscompute01")
        self.mock_nova.return_value.hypervisors.list.return_value = [remote]
        self._set_placement(
            {
                "oscompute01": {
                    "MEMORY_MB": (131072, 16384, 1.5, 0),
                }
            }
        )

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_hypervisors()

        hypervisor = models.Hypervisor.objects.get(
            settings=self.fixture.settings, backend_id="1"
        )
        self.assertEqual(hypervisor.memory_mb, 172032)

    def test_existing_hypervisor_is_updated(self):
        factories.HypervisorFactory(
            settings=self.fixture.settings,
            backend_id="1",
            name="old-name",
            vcpus=20,
        )
        remote = self._make_remote_hypervisor(1, "oscompute01")
        self.mock_nova.return_value.hypervisors.list.return_value = [remote]
        self._set_placement({"oscompute01": {"VCPU": (40, 0, 1.0, 0)}})

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_hypervisors()

        hypervisor = models.Hypervisor.objects.get(
            settings=self.fixture.settings, backend_id="1"
        )
        self.assertEqual(hypervisor.name, "oscompute01")
        self.assertEqual(hypervisor.vcpus, 40)

    def test_stale_hypervisor_is_deleted(self):
        factories.HypervisorFactory(
            settings=self.fixture.settings, backend_id="stale-id"
        )
        remote = self._make_remote_hypervisor(2, "oscompute02")
        self.mock_nova.return_value.hypervisors.list.return_value = [remote]

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_hypervisors()

        self.assertFalse(
            models.Hypervisor.objects.filter(
                settings=self.fixture.settings, backend_id="stale-id"
            ).exists()
        )
        self.assertTrue(
            models.Hypervisor.objects.filter(
                settings=self.fixture.settings, backend_id="2"
            ).exists()
        )

    def test_multiple_hypervisors_are_created(self):
        remotes = [
            self._make_remote_hypervisor(1, "oscompute01", "KVM"),
            self._make_remote_hypervisor(2, "oscompute02", "QEMU"),
            self._make_remote_hypervisor(3, "oscompute03", "ironic"),
        ]
        self.mock_nova.return_value.hypervisors.list.return_value = remotes

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_hypervisors()

        self.assertEqual(
            models.Hypervisor.objects.filter(settings=self.fixture.settings).count(), 3
        )

    def test_hypervisor_without_matching_placement_provider_gets_zero_capacity(
        self,
    ):
        # An ironic node or compute host whose Placement RP hasn't reported
        # yet should not crash the pull; capacity stays at zero until the next
        # cycle picks it up.
        remote = self._make_remote_hypervisor(1, "oscompute01")
        self.mock_nova.return_value.hypervisors.list.return_value = [remote]
        # Placement returns no resource providers
        self._set_placement({})

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_hypervisors()

        hypervisor = models.Hypervisor.objects.get(
            settings=self.fixture.settings, backend_id="1"
        )
        self.assertEqual(hypervisor.vcpus, 0)
        self.assertEqual(hypervisor.memory_mb, 0)


class PullServiceSettingsQuotasTest(test.APITestCase):
    """openstack_vcpu and openstack_ram quotas are now aggregated from
    Placement totals across all resource providers, replacing the removed
    nova.hypervisor_stats.statistics() endpoint."""

    def setUp(self):
        self.fixture = OpenStackFixture()
        self.placement_patcher = mock.patch(
            "waldur_openstack.backend.get_placement_client"
        )
        self.cinder_patcher = mock.patch("waldur_openstack.backend.get_cinder_client")
        self.mock_placement = self.placement_patcher.start()
        self.mock_cinder = self.cinder_patcher.start()
        self.mock_cinder.return_value.volumes.list.return_value = []
        self.mock_cinder.return_value.volume_snapshots.list.return_value = []
        mock_session()

    def tearDown(self):
        mock.patch.stopall()

    def test_vcpu_and_ram_aggregated_across_resource_providers(self):
        rps = [
            {"uuid": "rp-1", "name": "oscompute01"},
            {"uuid": "rp-2", "name": "oscompute02"},
        ]
        self.mock_placement.return_value.list_resource_providers.return_value = rps
        self.mock_placement.return_value.get_inventories.side_effect = lambda u: {
            "rp-1": {
                "VCPU": {"total": 40, "reserved": 0, "allocation_ratio": 1.0},
                "MEMORY_MB": {
                    "total": 131072,
                    "reserved": 0,
                    "allocation_ratio": 1.0,
                },
            },
            "rp-2": {
                "VCPU": {"total": 60, "reserved": 0, "allocation_ratio": 1.0},
                "MEMORY_MB": {
                    "total": 262144,
                    "reserved": 0,
                    "allocation_ratio": 1.0,
                },
            },
        }[u]
        self.mock_placement.return_value.get_usages.side_effect = lambda u: {
            "rp-1": {"VCPU": 5, "MEMORY_MB": 8192},
            "rp-2": {"VCPU": 12, "MEMORY_MB": 16384},
        }[u]

        backend = OpenStackBackend(self.fixture.settings)
        backend.pull_service_settings_quotas()

        self.assertEqual(self.fixture.settings.get_quota_limit("openstack_vcpu"), 100)
        self.assertEqual(self.fixture.settings.get_quota_usage("openstack_vcpu"), 17)
        self.assertEqual(self.fixture.settings.get_quota_limit("openstack_ram"), 393216)
        self.assertEqual(self.fixture.settings.get_quota_usage("openstack_ram"), 24576)
