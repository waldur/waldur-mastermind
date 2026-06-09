from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from . import factories, fixtures


class BaseSubNetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()


class SubNetPortSecurityFieldTest(BaseSubNetTest):
    def test_subnet_list_exposes_port_security_enabled_from_parent_network(self):
        self.fixture.network.port_security_enabled = False
        self.fixture.network.save(update_fields=["port_security_enabled"])
        subnet = self.fixture.subnet
        self.client.force_authenticate(self.fixture.admin)
        url = factories.SubNetFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        matching = [s for s in response.data if s["uuid"] == subnet.uuid.hex]
        self.assertEqual(len(matching), 1)
        self.assertFalse(matching[0]["port_security_enabled"])


@mock.patch("waldur_openstack.executors.SubNetDeleteExecutor.execute")
class SubNetDeleteActionTest(BaseSubNetTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.fixture.admin)
        self.url = factories.SubNetFactory.get_url(self.fixture.subnet)

    def test_subnet_delete_action_triggers_create_executor(self, executor_action_mock):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        executor_action_mock.assert_called_once()

    def test_subnet_delete_action_schedules_executor(self, executor_action_mock):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        executor_action_mock.assert_called_once()


class SubNetUpdateActionTest(BaseSubNetTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.fixture.admin)
        self.url = factories.SubNetFactory.get_url(self.fixture.subnet)
        self.request_data = {"name": "test_name"}

    @mock.patch("waldur_openstack.executors.SubNetUpdateExecutor.execute")
    def test_subnet_update_action_triggers_update_executor(self, executor_action_mock):
        response = self.client.put(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        executor_action_mock.assert_called_once()

    def test_subnet_update_does_not_reset_cidr(self):
        CIDR = "10.1.0.0/24"
        subnet = self.fixture.subnet
        subnet.cidr = CIDR
        subnet.save()

        response = self.client.put(self.url, self.request_data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        subnet.refresh_from_db()
        self.assertEqual(subnet.cidr, CIDR)

    def test_update_allocation_pools(self):
        CIDR = "192.168.42.0/29"
        subnet = self.fixture.subnet
        subnet.cidr = CIDR
        subnet.save()

        data = {
            "name": "test_name",
            "allocation_pools": [
                {
                    "start": "192.168.42.3",
                    "end": "192.168.42.6",
                }
            ],
        }
        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        subnet.refresh_from_db()

        self.assertEqual(
            subnet.allocation_pools,
            [
                {
                    "start": "192.168.42.3",
                    "end": "192.168.42.6",
                }
            ],
        )

    def test_validate_allocation_pools(self):
        CIDR = "192.168.42.0/29"
        subnet = self.fixture.subnet
        subnet.cidr = CIDR
        subnet.save()

        data = {
            "name": "test_name",
            "allocation_pools": [
                {
                    "start": "192.168.42.3",
                    "end": "192.168.42.8",
                }
            ],
        }
        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_update_cidr(self):
        """Test that subnet CIDR cannot be modified after creation."""
        # Start with a /24 subnet
        initial_cidr = "192.168.42.0/24"
        subnet = self.fixture.subnet
        subnet.cidr = initial_cidr
        subnet.save()

        # Try to modify the CIDR
        extended_cidr = "192.168.32.0/20"
        data = {
            "name": "test_name",
            "cidr": extended_cidr,
        }

        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # CIDR should not be changed
        subnet.refresh_from_db()
        self.assertEqual(subnet.cidr, initial_cidr)

    def test_multiple_non_overlapping_allocation_pools(self):
        """Test that multiple non-overlapping allocation pools are accepted."""
        CIDR = "192.168.42.0/24"
        subnet = self.fixture.subnet
        subnet.cidr = CIDR
        subnet.save()

        data = {
            "name": "test_name",
            "allocation_pools": [
                {
                    "start": "192.168.42.10",
                    "end": "192.168.42.50",
                },
                {
                    "start": "192.168.42.60",
                    "end": "192.168.42.100",
                },
                {
                    "start": "192.168.42.150",
                    "end": "192.168.42.200",
                },
            ],
        }

        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        subnet.refresh_from_db()
        self.assertEqual(len(subnet.allocation_pools), 3)

    def test_overlapping_allocation_pools_rejected(self):
        """Test that overlapping allocation pools are rejected."""
        CIDR = "192.168.42.0/24"
        subnet = self.fixture.subnet
        subnet.cidr = CIDR
        subnet.save()

        data = {
            "name": "test_name",
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

        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("overlap", str(response.data))

    def test_touching_allocation_pools_rejected(self):
        """Test that allocation pools that just touch each other are rejected."""
        CIDR = "192.168.42.0/24"
        subnet = self.fixture.subnet
        subnet.cidr = CIDR
        subnet.save()

        data = {
            "name": "test_name",
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

        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("overlap", str(response.data))


@ddt
class SubNetRBACTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = self.fixture.subnet
        self.subnet_2 = factories.SubNetFactory()
        self.url = factories.SubNetFactory.get_list_url()

    @data("admin", "owner")
    def test_user_can_filter_subnets_by_connection_type(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        factories.NetworkRBACPolicyFactory(
            network=self.subnet_2.network, target_tenant=self.fixture.tenant
        )

        response = self.client.get(
            self.url, {"tenant_uuid": self.fixture.tenant.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        response = self.client.get(
            self.url,
            {"tenant_uuid": self.fixture.tenant.uuid.hex, "direct_only": "true"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.subnet.uuid))

        response = self.client.get(
            self.url, {"tenant_uuid": self.fixture.tenant.uuid.hex, "rbac_only": "true"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.subnet_2.uuid))

    @data("admin", "owner")
    def test_user_cannot_update_subnet_shared_via_rbac(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        factories.NetworkRBACPolicyFactory(
            network=self.subnet_2.network, target_tenant=self.fixture.tenant
        )

        url = factories.SubNetFactory.get_url(self.subnet_2)

        response = self.client.put(url, {"name": "test_name"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("admin", "owner")
    def test_user_can_disconnect_own_subnet_but_not_shared_via_rbac(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        factories.NetworkRBACPolicyFactory(
            network=self.subnet_2.network, target_tenant=self.fixture.tenant
        )

        url = factories.SubNetFactory.get_url(self.subnet_2, "disconnect")

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        url = factories.SubNetFactory.get_url(self.subnet, "disconnect")

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
