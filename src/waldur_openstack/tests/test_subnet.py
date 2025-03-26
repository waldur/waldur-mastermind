from unittest import mock

from rest_framework import status, test

from . import factories, fixtures


class BaseSubNetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()


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

        data = {
            "name": "test_name",
            "cidr": CIDR,
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

        data = {
            "name": "test_name",
            "cidr": CIDR,
            "allocation_pools": [
                {
                    "start": "192.168.42.3",
                    "end": "192.168.42.8",
                }
            ],
        }
        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_extend_subnet_cidr(self):
        """Test that subnet CIDR can be extended from more specific to less specific."""
        # Start with a /24 subnet
        initial_cidr = "192.168.42.0/24"
        subnet = self.fixture.subnet
        subnet.cidr = initial_cidr
        subnet.save()

        # Extend to a /20 subnet (which contains the original /24)
        extended_cidr = "192.168.32.0/20"
        data = {
            "name": "test_name",
            "cidr": extended_cidr,
        }

        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        subnet.refresh_from_db()
        self.assertEqual(subnet.cidr, extended_cidr)

    def test_cannot_extend_to_overlapping_subnet(self):
        """Test that subnet CIDR cannot be changed to one that overlaps with another subnet."""
        # Create a second subnet in the same tenant
        # Variable necessary for test but not explicitly referenced
        # as it exists in the database to cause the overlap error we're testing
        factories.SubNetFactory(network=self.fixture.network, cidr="192.168.50.0/24")

        # Try to extend the first subnet to overlap with the second one
        overlapping_cidr = "192.168.0.0/16"  # This would contain both subnets
        data = {
            "name": "test_name",
            "cidr": overlapping_cidr,
        }

        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("overlaps", str(response.data))

    def test_multiple_non_overlapping_allocation_pools(self):
        """Test that multiple non-overlapping allocation pools are accepted."""
        CIDR = "192.168.42.0/24"
        subnet = self.fixture.subnet
        subnet.cidr = CIDR
        subnet.save()

        data = {
            "name": "test_name",
            "cidr": CIDR,
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
            "cidr": CIDR,
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
            "cidr": CIDR,
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

    def test_update_with_allocation_pools_after_extending_cidr(self):
        """Test updating allocation pools after extending the CIDR."""
        # Start with a /24 subnet
        initial_cidr = "192.168.42.0/24"
        subnet = self.fixture.subnet
        subnet.cidr = initial_cidr
        subnet.save()

        # Extend to a /20 subnet and update allocation pools
        extended_cidr = "192.168.32.0/20"
        data = {
            "name": "test_name",
            "cidr": extended_cidr,
            "allocation_pools": [
                {
                    "start": "192.168.32.10",
                    "end": "192.168.32.254",
                },
                {
                    "start": "192.168.33.10",
                    "end": "192.168.33.254",
                },
            ],
        }

        response = self.client.put(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        subnet.refresh_from_db()
        self.assertEqual(subnet.cidr, extended_cidr)
        self.assertEqual(len(subnet.allocation_pools), 2)
