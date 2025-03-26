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
