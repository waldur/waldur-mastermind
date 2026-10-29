from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core.enums import CoreStates

from . import factories, fixtures


class BaseNetworkTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()


class NetworkCreateActionTest(BaseNetworkTest):
    def test_network_create_action_is_not_allowed(self):
        self.client.force_authenticate(user=self.fixture.user)
        url = factories.NetworkFactory.get_list_url()

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class NetworkCreateSubnetActionTest(BaseNetworkTest):
    action_name = "create_subnet"
    quota_name = "subnet_count"

    def setUp(self):
        super().setUp()
        self.user = self.fixture.owner
        self.client.force_authenticate(self.user)
        self.url = factories.NetworkFactory.get_url(
            network=self.fixture.network, action=self.action_name
        )
        self.request_data = {
            "name": "test_subnet_name",
        }

    def test_create_subnet_is_not_allowed_when_state_is_not_OK(self):
        self.fixture.network.state = CoreStates.ERRED
        self.fixture.network.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_cannot_create_subnet_when_network_has_one_already(self):
        factories.SubNetFactory(network=self.fixture.network)
        self.assertEqual(self.fixture.network.subnets.count(), 1)

        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_create_subnet_triggers_create_executor(self, executor_action_mock):
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        executor_action_mock.assert_called_once()

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_create_subnet_increases_quota_usage(self, executor_action_mock):
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.fixture.tenant.get_quota_usage(self.quota_name), 1)
        executor_action_mock.assert_called_once()

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_create_subnet_does_not_create_subnet_if_quota_exceeds_set_limit(
        self, executor_action_mock
    ):
        self.fixture.tenant.set_quota_limit(self.quota_name, 0)
        response = self.client.post(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.fixture.tenant.get_quota_usage(self.quota_name), 0)
        executor_action_mock.assert_not_called()

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_subnet_is_not_created_if_cidr_overlaps(self, executor_action_mock):
        subnet = factories.SubNetFactory(
            network=self.fixture.network,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            cidr="192.168.42.0/24",
        )
        response = self.client.post(
            self.url, dict(cidr=subnet.cidr, **self.request_data)
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        executor_action_mock.assert_not_called()

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_subnet_is_created_if_cidr_do_not_overlap_in_current_tenant(
        self, executor_action_mock
    ):
        subnet = factories.SubNetFactory(
            project=self.fixture.project, cidr="192.168.42.0/24"
        )
        response = self.client.post(
            self.url, dict(cidr=subnet.cidr, **self.request_data)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        executor_action_mock.assert_called_once()

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_create_subnet_with_allocation_pools(self, executor_action_mock):
        """Test creating a subnet with allocation pools."""
        data = {
            "name": "test_subnet_name",
            "cidr": "192.168.42.0/24",
            "allocation_pools": [
                {
                    "start": "192.168.42.10",
                    "end": "192.168.42.50",
                },
                {
                    "start": "192.168.42.60",
                    "end": "192.168.42.100",
                },
            ],
        }
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        executor_action_mock.assert_called_once()

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_create_subnet_with_overlapping_allocation_pools(
        self, executor_action_mock
    ):
        """Test that creating a subnet with overlapping allocation pools is rejected."""
        data = {
            "name": "test_subnet_name",
            "cidr": "192.168.42.0/24",
            "allocation_pools": [
                {
                    "start": "192.168.42.10",
                    "end": "192.168.42.50",
                },
                {
                    "start": "192.168.42.40",  # Overlaps with the first pool
                    "end": "192.168.42.100",
                },
            ],
        }
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("overlap", str(response.data))
        executor_action_mock.assert_not_called()

    @mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute")
    def test_create_subnet_with_touching_allocation_pools(self, executor_action_mock):
        """Test that creating a subnet with touching allocation pools is rejected."""
        data = {
            "name": "test_subnet_name",
            "cidr": "192.168.42.0/24",
            "allocation_pools": [
                {
                    "start": "192.168.42.10",
                    "end": "192.168.42.50",
                },
                {
                    "start": "192.168.42.50",  # Same as the end of the first pool
                    "end": "192.168.42.100",
                },
            ],
        }
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("overlap", str(response.data))
        executor_action_mock.assert_not_called()


class NetworkUpdateActionTest(BaseNetworkTest):
    def setUp(self):
        super().setUp()
        self.user = self.fixture.owner
        self.client.force_authenticate(self.user)
        self.request_data = {
            "name": "test_name",
        }

    @mock.patch("waldur_openstack.executors.NetworkUpdateExecutor.execute")
    def test_update_action_triggers_update_executor(self, executor_action_mock):
        url = factories.NetworkFactory.get_url(network=self.fixture.network)
        response = self.client.put(url, self.request_data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        executor_action_mock.assert_called_once()


@mock.patch("waldur_openstack.executors.NetworkDeleteExecutor.execute")
class NetworkDeleteActionTest(BaseNetworkTest):
    def setUp(self):
        super().setUp()
        self.user = self.fixture.owner
        self.client.force_authenticate(self.user)
        self.request_data = {
            "name": "test_name",
        }

    def test_delete_action_triggers_delete_executor(self, executor_action_mock):
        url = factories.NetworkFactory.get_url(network=self.fixture.network)
        response = self.client.delete(url, self.request_data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        executor_action_mock.assert_called_once()

    def test_delete_action_decreases_quota_usage(self, executor_action_mock):
        url = factories.NetworkFactory.get_url(network=self.fixture.network)
        self.fixture.network.increase_backend_quotas_usage()
        self.assertEqual(self.fixture.tenant.get_quota_usage("network_count"), 1)

        response = self.client.delete(url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        executor_action_mock.assert_called_once()


@ddt
class NetworkFieldsFilterTest(BaseNetworkTest):
    def setUp(self):
        super().setUp()
        self.network = self.fixture.network
        self.url = factories.NetworkFactory.get_url(self.network)

    @data("staff", "global_support")
    def test_user_can_get_field(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue("segmentation_id" in response.data)

    @data("admin", "owner")
    def test_user_can_not_get_field(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse("segmentation_id" in response.data)


class NetworkPortSecurityFieldTest(BaseNetworkTest):
    def setUp(self):
        super().setUp()
        self.network = self.fixture.network
        self.subnet = self.fixture.subnet

    def test_network_detail_exposes_port_security_enabled(self):
        self.network.port_security_enabled = False
        self.network.save(update_fields=["port_security_enabled"])
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(factories.NetworkFactory.get_url(self.network))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["port_security_enabled"])

    def test_nested_subnet_projects_port_security_enabled_from_network(self):
        self.network.port_security_enabled = False
        self.network.save(update_fields=["port_security_enabled"])
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(factories.NetworkFactory.get_url(self.network))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        subnet_uuid = self.subnet.uuid.hex
        matching = [s for s in response.data["subnets"] if s["uuid"] == subnet_uuid]
        self.assertEqual(len(matching), 1)
        self.assertFalse(matching[0]["port_security_enabled"])


@ddt
class NetworkRBACTest(test.APITestCase):
    def setUp(self):
        self.fixture_1 = fixtures.OpenStackFixture()
        self.fixture_2 = fixtures.OpenStackFixture()
        self.network_1 = self.fixture_1.network
        self.network_2 = self.fixture_2.network
        self.url = factories.NetworkFactory.get_list_url()

    @data("admin", "owner")
    def test_user_can_see_own_networks_and_shared_via_rbac(self, user):
        self.client.force_authenticate(getattr(self.fixture_1, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        factories.NetworkRBACPolicyFactory(
            network=self.fixture_2.network, target_tenant=self.fixture_1.tenant
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    @data("admin", "owner")
    def test_user_can_filter_networks_by_tenant_uuid(self, user):
        self.client.force_authenticate(getattr(self.fixture_1, user))
        factories.NetworkRBACPolicyFactory(
            network=self.fixture_2.network, target_tenant=self.fixture_1.tenant
        )
        response = self.client.get(
            self.url, {"tenant_uuid": self.fixture_1.tenant.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    @data("admin", "owner")
    def test_user_can_update_own_network_but_not_shared_via_rbac(self, user):
        self.client.force_authenticate(getattr(self.fixture_1, user))
        factories.NetworkRBACPolicyFactory(
            network=self.fixture_2.network, target_tenant=self.fixture_1.tenant
        )
        url = factories.NetworkFactory.get_url(self.fixture_1.network)
        response = self.client.patch(url, {"name": "new"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        url = factories.NetworkFactory.get_url(self.fixture_2.network)
        response = self.client.patch(url, {"name": "new"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("admin", "owner")
    def test_user_can_set_mtu_for_own_network_but_not_shared_via_rbac(self, user):
        self.client.force_authenticate(getattr(self.fixture_1, user))
        factories.NetworkRBACPolicyFactory(
            network=self.fixture_2.network, target_tenant=self.fixture_1.tenant
        )
        url = factories.NetworkFactory.get_url(self.fixture_1.network, "set_mtu")
        response = self.client.post(url, {"mtu": 1234})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        url = factories.NetworkFactory.get_url(self.fixture_2.network, "set_mtu")
        response = self.client.post(url, {"mtu": 1234})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("admin", "owner")
    def test_user_can_filter_networks_by_connection_type(self, user):
        self.client.force_authenticate(getattr(self.fixture_1, user))
        factories.NetworkRBACPolicyFactory(
            network=self.fixture_2.network, target_tenant=self.fixture_1.tenant
        )

        response = self.client.get(
            self.url,
            {"tenant_uuid": self.fixture_1.tenant.uuid.hex, "direct_only": "true"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.fixture_1.network.uuid))

        response = self.client.get(
            self.url,
            {"tenant_uuid": self.fixture_1.tenant.uuid.hex, "rbac_only": "true"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.fixture_2.network.uuid))

        response = self.client.get(
            self.url, {"tenant_uuid": self.fixture_1.tenant.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)


@ddt
class NetworkWritePermissionTest(BaseNetworkTest):
    """Changing or deleting a network, or adding a subnet to it, is for those
    who manage its project, as every other write is."""

    def setUp(self):
        super().setUp()
        self.network = self.fixture.network
        self.executors = {}
        for name in (
            "NetworkUpdateExecutor",
            "NetworkDeleteExecutor",
            "SetMtuExecutor",
            "SubNetCreateExecutor",
        ):
            patcher = mock.patch(f"waldur_openstack.executors.{name}.execute")
            self.addCleanup(patcher.stop)
            self.executors[name] = patcher.start()

    def url(self, action=None):
        return factories.NetworkFactory.get_url(self.network, action)

    @data("member", "customer_support", "global_support")
    def test_who_only_sees_it_cannot_change_it(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        for method, url, payload in (
            ("put", self.url(), {"name": "renamed"}),
            ("patch", self.url(), {"name": "renamed"}),
            ("post", self.url("set_mtu"), {"mtu": 1400}),
            ("post", self.url("create_subnet"), {"name": "subnet"}),
            ("delete", self.url(), None),
        ):
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url, payload, format="json")
                self.assertEqual(
                    response.status_code, status.HTTP_403_FORBIDDEN, response.data
                )
        for execute in self.executors.values():
            execute.assert_not_called()
        self.network.refresh_from_db()
        self.assertNotEqual(self.network.name, "renamed")
        self.assertFalse(self.network.subnets.filter(name="subnet").exists())

    @data("staff", "owner", "admin", "manager")
    def test_who_manages_its_project_can_change_it(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        renamed = self.client.put(self.url(), {"name": "renamed"}, format="json")
        mtu = self.client.post(self.url("set_mtu"), {"mtu": 1400}, format="json")
        deleted = self.client.delete(self.url())

        self.assertEqual(renamed.status_code, status.HTTP_200_OK, renamed.data)
        self.assertEqual(mtu.status_code, status.HTTP_202_ACCEPTED, mtu.data)
        self.assertEqual(deleted.status_code, status.HTTP_202_ACCEPTED, deleted.data)

    @data("staff", "owner", "admin", "manager")
    def test_who_manages_its_project_can_add_a_subnet(self, user):
        self.network.subnets.all().delete()
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(
            self.url("create_subnet"), {"name": "subnet"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.executors["SubNetCreateExecutor"].assert_called_once()
