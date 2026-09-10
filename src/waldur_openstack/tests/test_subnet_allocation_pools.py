"""A subnet has to know its own address pool (#390).

`create_subnet` sent `allocation_pools` only when the caller supplied one and
threw away what Neutron answered, while the model's default was `dict`. A subnet
created through the API therefore carried `{}` until the next pull -- up to two
hours -- and `get_free_ip` reads that field, so `add_router_interface` reported
the brand-new subnet as having no free addresses at all. The same stale value was
pushed back to Neutron by any later update, which would ask it to drop a pool the
tenant is using.
"""

from unittest import mock

from rest_framework import test

from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend

from . import factories, fixtures

BACKEND_POOLS = [{"start": "192.168.99.2", "end": "192.168.99.254"}]


class SubNetModelDefaultTest(test.APITestCase):
    def test_the_default_is_an_empty_list(self):
        """The serializer, the schema and every generated client call this an
        array; `{}` is not one."""
        self.assertEqual(models.SubNet().allocation_pools, [])


class CreateSubnetKeepsThePoolTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.tenant.backend_id = "tenant-backend-id"
        self.tenant.save()
        self.network = factories.NetworkFactory(
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="network-backend-id",
        )
        self.subnet = factories.SubNetFactory(
            network=self.network,
            tenant=self.tenant,
            service_settings=self.tenant.service_settings,
            project=self.tenant.project,
            backend_id="",
            cidr="192.168.99.0/24",
            allocation_pools=[],
        )
        self.backend = OpenStackBackend(self.tenant.service_settings)

    def _create(self, response_subnet):
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
            mock.patch.object(self.backend, "connect_subnet"),
            mock.patch.object(self.backend, "pull_tenant_routers"),
            mock.patch.object(self.backend, "import_new_router_interface"),
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.create_subnet.return_value = {"subnet": response_subnet}
            self.backend.create_subnet(self.subnet)
            self.subnet.refresh_from_db()
            return client

    def test_the_pool_neutron_allocated_is_stored(self):
        self._create(
            {
                "id": "subnet-backend-id",
                "gateway_ip": "192.168.99.1",
                "allocation_pools": BACKEND_POOLS,
            }
        )

        self.assertEqual(self.subnet.allocation_pools, BACKEND_POOLS)

    def test_a_response_without_a_pool_leaves_the_field_alone(self):
        """Nothing to copy is not a reason to overwrite a requested pool."""
        self.subnet.allocation_pools = BACKEND_POOLS
        self.subnet.save()

        self._create({"id": "subnet-backend-id", "gateway_ip": "192.168.99.1"})

        self.assertEqual(self.subnet.allocation_pools, BACKEND_POOLS)

    def test_a_requested_pool_is_still_sent(self):
        self.subnet.allocation_pools = BACKEND_POOLS
        self.subnet.save()

        client = self._create(
            {
                "id": "subnet-backend-id",
                "gateway_ip": "192.168.99.1",
                "allocation_pools": BACKEND_POOLS,
            }
        )

        payload = client.create_subnet.call_args[0][0]["subnet"]
        self.assertEqual(payload["allocation_pools"], BACKEND_POOLS)


class UpdateSubnetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = self.fixture.subnet
        self.subnet.backend_id = "subnet-backend-id"
        self.subnet.save()
        self.backend = OpenStackBackend(self.fixture.settings)

    def _update(self):
        with (
            mock.patch("waldur_openstack.backend.get_tenant_session"),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.show_subnet.return_value = {
                "subnet": {
                    "gateway_ip": self.subnet.gateway_ip,
                    "cidr": self.subnet.cidr,
                    "allocation_pools": BACKEND_POOLS,
                }
            }
            self.backend.update_subnet(self.subnet)
            return client.update_subnet.call_args[0][1]["subnet"]

    def test_an_unknown_pool_is_not_pushed(self):
        """The regression: a rename would have asked Neutron to drop the pool the
        tenant is using, because the empty local value 'differs'."""
        self.subnet.allocation_pools = []
        self.subnet.save()

        self.assertNotIn("allocation_pools", self._update())

    def test_the_old_dict_shaped_value_is_not_pushed_either(self):
        self.subnet.allocation_pools = {}
        self.subnet.save()

        self.assertNotIn("allocation_pools", self._update())

    def test_a_real_change_is_still_pushed(self):
        wanted = [{"start": "192.168.42.10", "end": "192.168.42.20"}]
        self.subnet.allocation_pools = wanted
        self.subnet.save()

        self.assertEqual(self._update()["allocation_pools"], wanted)


class GetFreeIpTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = self.fixture.subnet
        self.subnet.backend_id = "subnet-backend-id"
        self.subnet.cidr = "192.168.99.0/24"
        self.subnet.save()
        self.backend = OpenStackBackend(self.fixture.settings)

    def _get_free_ip(self, used=()):
        with (
            mock.patch(
                "waldur_openstack.backend.OpenStackBackend.admin_session",
                new_callable=mock.PropertyMock,
            ),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_ports.return_value = {
                "ports": [
                    {
                        "fixed_ips": [
                            {"subnet_id": self.subnet.backend_id, "ip_address": ip}
                        ]
                    }
                    for ip in used
                ]
            }
            client.show_subnet.return_value = {
                "subnet": {"allocation_pools": BACKEND_POOLS}
            }
            return self.backend.get_free_ip(self.subnet), client

    def test_a_row_with_no_pool_asks_the_backend(self):
        """What every subnet created before this fix looks like -- and what made
        add_router_interface answer 'No available IP addresses'."""
        self.subnet.allocation_pools = {}
        self.subnet.save()

        free_ip, client = self._get_free_ip()

        self.assertEqual(free_ip, "192.168.99.2")
        client.show_subnet.assert_called_once_with(self.subnet.backend_id)

    def test_a_known_pool_costs_no_extra_call(self):
        self.subnet.allocation_pools = BACKEND_POOLS
        self.subnet.save()

        free_ip, client = self._get_free_ip()

        self.assertEqual(free_ip, "192.168.99.2")
        client.show_subnet.assert_not_called()

    def test_addresses_already_taken_are_skipped(self):
        self.subnet.allocation_pools = BACKEND_POOLS
        self.subnet.save()

        free_ip, _ = self._get_free_ip(used=["192.168.99.2", "192.168.99.3"])

        self.assertEqual(free_ip, "192.168.99.4")

    def test_a_backend_without_a_pool_still_reports_nothing_free(self):
        self.subnet.allocation_pools = []
        self.subnet.save()

        with (
            mock.patch(
                "waldur_openstack.backend.OpenStackBackend.admin_session",
                new_callable=mock.PropertyMock,
            ),
            mock.patch("waldur_openstack.backend.get_neutron_client") as get_client,
        ):
            client = mock.MagicMock()
            get_client.return_value = client
            client.list_ports.return_value = {"ports": []}
            client.show_subnet.return_value = {"subnet": {"allocation_pools": []}}

            self.assertIsNone(self.backend.get_free_ip(self.subnet))
