from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openstack import models as openstack_models
from waldur_openstack.tests import factories as openstack_factories

LIST_URL = "/api/marketplace-stats/openstack_instances/"
AGGREGATE_URL = "/api/marketplace-stats/openstack_instances_aggregate/"


class BaseOpenStackInstanceReportTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.settings = openstack_factories.SettingsFactory(
            customer=self.fixture.customer,
            state=CoreStates.OK,
        )
        self.tenant = openstack_factories.TenantFactory(
            service_settings=self.settings,
            project=self.fixture.project,
            state=CoreStates.OK,
        )
        self.az = openstack_factories.InstanceAvailabilityZoneFactory(
            tenant=self.tenant,
            settings=self.settings,
            name="az-1",
        )
        self.instance = openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            state=CoreStates.OK,
            runtime_state=openstack_models.Instance.RuntimeStates.ACTIVE,
            cores=4,
            ram=8192,
            disk=40960,
            flavor_name="m1.large",
            image_name="Ubuntu 22.04",
            hypervisor_hostname="compute-01.cloud",
            availability_zone=self.az,
        )
        # Create a port with floating IP for network info
        self.port = openstack_factories.PortFactory(
            instance=self.instance,
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.fixture.project,
            fixed_ips=[{"ip_address": "10.0.0.5", "subnet_id": "sub1"}],
        )
        self.floating_ip = openstack_factories.FloatingIPFactory(
            port=self.port,
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.fixture.project,
            address="203.0.113.10",
        )
        # InstanceFactory creates 2 volumes via post_generation:
        # system (10240 MiB) and data (20480 MiB), total 30720 MiB
        # Create an additional volume attached to the instance
        self.volume = openstack_factories.VolumeFactory(
            instance=self.instance,
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.fixture.project,
            size=20480,
        )


class OpenStackInstancesListPermissionTest(BaseOpenStackInstanceReportTest):
    def test_staff_can_access(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_can_access(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_regular_user_gets_403(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_gets_401(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class OpenStackInstancesListTest(BaseOpenStackInstanceReportTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.fixture.staff)

    def test_response_contains_all_fields(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        item = response.data[0]
        expected_fields = {
            "uuid",
            "name",
            "created",
            "cores",
            "ram",
            "disk",
            "flavor_name",
            "flavor_disk",
            "image_name",
            "hypervisor_hostname",
            "runtime_state",
            "state",
            "availability_zone_name",
            "start_time",
            "service_settings_uuid",
            "service_settings_name",
            "tenant_uuid",
            "tenant_name",
            "project_uuid",
            "project_name",
            "customer_uuid",
            "customer_name",
            "customer_abbreviation",
            "volume_count",
            "total_volume_size_mb",
            "floating_ip_count",
            "port_count",
            "internal_ips",
            "external_ips",
        }
        self.assertEqual(set(item.keys()), expected_fields)

    def test_response_values(self):
        response = self.client.get(LIST_URL)
        item = response.data[0]
        self.assertEqual(item["cores"], 4)
        self.assertEqual(item["ram"], 8192)
        self.assertEqual(item["disk"], 40960)
        self.assertEqual(item["flavor_name"], "m1.large")
        self.assertEqual(item["image_name"], "Ubuntu 22.04")
        self.assertEqual(item["hypervisor_hostname"], "compute-01.cloud")
        self.assertEqual(item["runtime_state"], "ACTIVE")
        self.assertEqual(item["state"], "OK")
        self.assertEqual(item["availability_zone_name"], "az-1")
        # 2 from factory + 1 manually created
        self.assertEqual(item["volume_count"], 3)
        # factory: 10240 + 20480 = 30720, plus manual 20480 = 51200
        self.assertEqual(item["total_volume_size_mb"], 51200)
        self.assertEqual(item["floating_ip_count"], 1)
        self.assertEqual(item["port_count"], 1)
        self.assertIn("10.0.0.5", item["internal_ips"])
        self.assertIn("203.0.113.10", item["external_ips"])

    def test_filter_by_flavor_name(self):
        openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            flavor_name="m1.small",
        )
        response = self.client.get(LIST_URL, {"flavor_name": "large"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["flavor_name"], "m1.large")

    def test_filter_by_hypervisor_hostname(self):
        openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            hypervisor_hostname="compute-02.cloud",
        )
        response = self.client.get(LIST_URL, {"hypervisor_hostname": "compute-01"})
        self.assertEqual(len(response.data), 1)

    def test_filter_by_customer_uuid(self):
        other_fixture = structure_fixtures.ProjectFixture()
        other_settings = openstack_factories.SettingsFactory(
            customer=other_fixture.customer,
        )
        other_tenant = openstack_factories.TenantFactory(
            service_settings=other_settings,
            project=other_fixture.project,
        )
        openstack_factories.InstanceFactory(
            project=other_fixture.project,
            tenant=other_tenant,
            service_settings=other_settings,
        )
        response = self.client.get(
            LIST_URL, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["customer_uuid"], str(self.fixture.customer.uuid)
        )

    def test_filter_by_runtime_state(self):
        openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            runtime_state=openstack_models.Instance.RuntimeStates.SHUTOFF,
        )
        response = self.client.get(LIST_URL, {"runtime_state": "ACTIVE"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["runtime_state"], "ACTIVE")

    def test_filter_by_cores_range(self):
        openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            cores=2,
        )
        response = self.client.get(LIST_URL, {"cores_min": 3})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["cores"], 4)

    def test_pagination_headers(self):
        response = self.client.get(LIST_URL, {"page_size": 1})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("X-Result-Count", response)

    def test_ordering_by_name(self):
        openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            name="aaa-first",
        )
        response = self.client.get(LIST_URL, {"o": "name"})
        names = [item["name"] for item in response.data]
        self.assertEqual(names, sorted(names))

    def test_ordering_descending(self):
        openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            cores=16,
        )
        response = self.client.get(LIST_URL, {"o": "-cores"})
        cores = [item["cores"] for item in response.data]
        self.assertEqual(cores, sorted(cores, reverse=True))


class OpenStackInstancesAggregatePermissionTest(BaseOpenStackInstanceReportTest):
    def test_staff_can_access(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(AGGREGATE_URL, {"group_by": "flavor_name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_can_access(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(AGGREGATE_URL, {"group_by": "flavor_name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_regular_user_gets_403(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(AGGREGATE_URL, {"group_by": "flavor_name"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class OpenStackInstancesAggregateTest(BaseOpenStackInstanceReportTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.fixture.staff)

    def test_group_by_is_required(self):
        response = self.client.get(AGGREGATE_URL)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_group_by_rejected(self):
        response = self.client.get(AGGREGATE_URL, {"group_by": "invalid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_group_by_flavor_name(self):
        openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            flavor_name="m1.large",
            cores=4,
            ram=8192,
            disk=40960,
        )
        response = self.client.get(AGGREGATE_URL, {"group_by": "flavor_name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row["group_key"], "m1.large")
        self.assertEqual(row["instance_count"], 2)
        self.assertEqual(row["total_cores"], 8)
        self.assertEqual(row["total_ram_mb"], 16384)

    def test_group_by_runtime_state(self):
        openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            runtime_state=openstack_models.Instance.RuntimeStates.SHUTOFF,
        )
        response = self.client.get(AGGREGATE_URL, {"group_by": "runtime_state"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        states = {row["group_key"] for row in response.data}
        self.assertIn("ACTIVE", states)
        self.assertIn("SHUTOFF", states)

    def test_group_by_customer(self):
        response = self.client.get(AGGREGATE_URL, {"group_by": "customer"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row["group_key"], str(self.fixture.customer.uuid))
        self.assertEqual(row["group_label"], self.fixture.customer.name)

    def test_group_by_hypervisor(self):
        openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            hypervisor_hostname="compute-02.cloud",
            cores=8,
        )
        response = self.client.get(AGGREGATE_URL, {"group_by": "hypervisor_hostname"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_filters_narrow_aggregation(self):
        openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.tenant,
            service_settings=self.settings,
            runtime_state=openstack_models.Instance.RuntimeStates.SHUTOFF,
            flavor_name="m1.small",
        )
        response = self.client.get(
            AGGREGATE_URL,
            {"group_by": "flavor_name", "runtime_state": "ACTIVE"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["group_key"], "m1.large")

    def test_response_contains_all_fields(self):
        response = self.client.get(AGGREGATE_URL, {"group_by": "flavor_name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data[0]
        expected_fields = {
            "group_key",
            "group_label",
            "instance_count",
            "total_cores",
            "total_ram_mb",
            "total_disk_mb",
            "total_volume_size_mb",
            "total_floating_ips",
        }
        self.assertEqual(set(row.keys()), expected_fields)

    def test_volume_and_floating_ip_aggregation(self):
        response = self.client.get(AGGREGATE_URL, {"group_by": "flavor_name"})
        row = response.data[0]
        # factory: 10240 + 20480 = 30720, plus manual 20480 = 51200
        self.assertEqual(row["total_volume_size_mb"], 51200)
        self.assertEqual(row["total_floating_ips"], 1)
