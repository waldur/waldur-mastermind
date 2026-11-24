from unittest import mock

from django.core.management import call_command
from django.db import transaction
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import utils
from waldur_openstack import models as openstack_models
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures as openstack_fixtures


class MappedAddressesTest(test.APITestCase):
    """Floating IPs in 10.80.0.0/24 are 1:1 NATed into 195.80.105.0/24."""

    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.settings = self.fixture.settings
        self.subnet = openstack_factories.ExternalSubnetFactory(
            network=openstack_factories.ExternalNetworkFactory(settings=self.settings),
            cidr="10.80.0.0/24",
            public_ip_range="195.80.105.0/24",
        )
        self.tenant_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, scope=self.settings
        )
        self.tenant_resource = marketplace_factories.ResourceFactory(
            offering=self.tenant_offering,
            project=self.fixture.project,
            scope=self.tenant,
        )
        self.instance_resource = marketplace_factories.ResourceFactory(
            offering=marketplace_factories.OfferingFactory(
                type=OPENSTACK_INSTANCE_OFFERING,
                scope=self.settings,
                parent=self.tenant_offering,
            ),
            project=self.fixture.project,
            scope=self.fixture.instance,
        )
        self.vm_port = self.fixture.port
        self.client.force_authenticate(self.fixture.staff)

    def _port(self, ip, **kwargs):
        return openstack_factories.PortFactory(
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.fixture.project,
            fixed_ips=[{"ip_address": ip, "subnet_id": "subnet"}],
            **kwargs,
        )

    def _floating_ip(self, address, port=None):
        return openstack_factories.FloatingIPFactory(
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.fixture.project,
            address=address,
            port=port,
        )

    def _search(self, value):
        response = self.client.get(
            marketplace_factories.ResourceFactory.get_list_url(), {"query": value}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def _refresh(self, *objects):
        for obj in objects:
            obj.refresh_from_db()

    def test_floating_ip_is_mapped_whatever_it_is_attached_to(self):
        cases = {
            "10.80.0.11": self.vm_port,
            "10.80.0.12": None,
            "10.80.0.14": self._port("192.168.1.50", device_owner="Octavia"),
            "10.80.0.15": self._port("192.168.1.60"),
        }
        for address, port in cases.items():
            floating_ip = self._floating_ip(address, port)
            floating_ip.refresh_from_db()
            self.assertEqual(
                floating_ip.external_address,
                address.replace("10.80.0.", "195.80.105."),
            )

    def test_address_outside_mapped_range_is_not_mapped(self):
        floating_ip = self._floating_ip("10.99.0.12")
        floating_ip.refresh_from_db()
        self.assertIsNone(floating_ip.external_address)

    def test_search_finds_vm_by_mapped_ip_of_its_floating_ip(self):
        self._floating_ip("10.80.0.11", self.vm_port)
        self.assertEqual(
            self._search("195.80.105.11"), {self.instance_resource.uuid.hex}
        )

    def test_search_finds_tenant_by_addresses_not_on_a_vm(self):
        lb_port = self._port("192.168.1.50", device_owner="Octavia")
        openstack_factories.LoadBalancerFactory(
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.fixture.project,
            vip_address="192.168.1.50",
            vip_port=lb_port,
        )
        self._floating_ip("10.80.0.12")
        self._floating_ip("10.80.0.14", lb_port)
        self._floating_ip("10.80.0.15", self._port("192.168.1.60"))
        openstack_factories.RouterFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            fixed_ips=["192.168.1.1", "10.80.0.13"],
        )

        for value in (
            "195.80.105.12",  # unattached floating IP, mapped
            "10.80.0.12",  # unattached floating IP
            "195.80.105.13",  # router gateway, mapped
            "195.80.105.14",  # load balancer VIP floating IP, mapped
            "195.80.105.15",  # floating IP on a port with no VM, mapped
            "192.168.1.50",  # load balancer VIP
            "192.168.1.60",  # port with no VM
        ):
            with self.subTest(value=value):
                self.assertEqual(self._search(value), {self.tenant_resource.uuid.hex})

    def test_vm_addresses_are_not_indexed_on_tenant(self):
        self._floating_ip("10.80.0.11", self.vm_port)
        self.tenant_resource.refresh_from_db()
        self.assertNotIn(
            "195.80.105.11", self.tenant_resource.backend_metadata["ip_addresses"]
        )

    def test_changing_public_ip_range_remaps_existing_floating_ips(self):
        vm_ip = self._floating_ip("10.80.0.11", self.vm_port)
        free_ip = self._floating_ip("10.80.0.12")

        self.subnet.public_ip_range = "195.80.106.0/24"
        self.subnet.save()

        self._refresh(vm_ip, free_ip)
        self.assertEqual(vm_ip.external_address, "195.80.106.11")
        self.assertEqual(free_ip.external_address, "195.80.106.12")
        self.assertEqual(
            self._search("195.80.106.11"), {self.instance_resource.uuid.hex}
        )
        self.assertEqual(self._search("195.80.106.12"), {self.tenant_resource.uuid.hex})
        self.assertEqual(self._search("195.80.105.11"), set())

    def test_detaching_floating_ip_moves_it_from_vm_to_tenant(self):
        floating_ip = self._floating_ip("10.80.0.11", self.vm_port)
        floating_ip.port = None
        floating_ip.save()

        self.assertEqual(self._search("195.80.105.11"), {self.tenant_resource.uuid.hex})
        self.instance_resource.refresh_from_db()
        self.assertEqual(
            self.instance_resource.backend_metadata["external_address"], []
        )

    def test_deleting_floating_ip_removes_it_from_search(self):
        floating_ip = self._floating_ip("10.80.0.12")
        floating_ip.delete()
        self.assertEqual(self._search("195.80.105.12"), set())

    def test_offering_mapping_change_remaps_unattached_floating_ip(self):
        self.subnet.public_ip_range = ""
        self.subnet.save()
        floating_ip = self._floating_ip("10.80.0.12")

        self.tenant_offering.secret_options = {
            "ipv4_external_ip_mapping": [
                {"floating_ip": "10.80.0.0/24", "external_ip": "203.0.113.0/24"}
            ]
        }
        self.tenant_offering.save()

        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.external_address, "203.0.113.12")
        self.assertEqual(self._search("203.0.113.12"), {self.tenant_resource.uuid.hex})

    def test_command_backfills_floating_ips_created_before_mapping(self):
        floating_ip = self._floating_ip("10.80.0.12")
        # As left by earlier versions, which never mapped unattached floating IPs.
        openstack_models.FloatingIP.objects.filter(pk=floating_ip.pk).update(
            external_address=None
        )
        marketplace_models.Resource.objects.filter(pk=self.tenant_resource.pk).update(
            backend_metadata={}
        )

        call_command("update_openstack_external_addresses")

        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.external_address, "195.80.105.12")
        self.assertEqual(self._search("195.80.105.12"), {self.tenant_resource.uuid.hex})

    def test_port_returns_floating_ip_and_mapped_address(self):
        port = self._port("192.168.1.60")
        self._floating_ip("10.80.0.15", port)

        response = self.client.get(openstack_factories.PortFactory.get_url(port))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [
                (item["address"], item["external_address"])
                for item in response.data["floating_ip_addresses"]
            ],
            [("10.80.0.15", "195.80.105.15")],
        )

    def test_port_and_floating_ip_lists_can_be_searched_by_mapped_address(self):
        port = self._port("192.168.1.60")
        floating_ip = self._floating_ip("10.80.0.15", port)

        response = self.client.get(
            openstack_factories.PortFactory.get_list_url(), {"query": "195.80.105.15"}
        )
        self.assertEqual([item["uuid"] for item in response.data], [port.uuid.hex])

        response = self.client.get(
            openstack_factories.FloatingIPFactory.get_list_url(),
            {"query": "195.80.105.15"},
        )
        self.assertEqual(
            [item["uuid"] for item in response.data], [floating_ip.uuid.hex]
        )

    def test_other_customers_cannot_find_tenant_by_mapped_address(self):
        self._floating_ip("10.80.0.12")
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assertEqual(self._search("195.80.105.12"), set())

    def test_moving_port_to_another_vm_moves_its_mapped_address(self):
        self._floating_ip("10.80.0.11", self.vm_port)
        other_vm = openstack_factories.InstanceFactory(
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.fixture.project,
        )
        other_resource = marketplace_factories.ResourceFactory(
            offering=self.instance_resource.offering,
            project=self.fixture.project,
            scope=other_vm,
        )

        self.vm_port.instance = other_vm
        self.vm_port.save()

        self.assertEqual(self._search("195.80.105.11"), {other_resource.uuid.hex})

    def test_deleting_external_subnet_unmaps_floating_ips(self):
        floating_ip = self._floating_ip("10.80.0.12")
        self.subnet.delete()

        floating_ip.refresh_from_db()
        self.assertIsNone(floating_ip.external_address)
        self.assertEqual(self._search("195.80.105.12"), set())

    def test_malformed_public_range_does_not_break_saves(self):
        self.subnet.public_ip_range = "195.80.105.0/33"
        self.subnet.save()
        router = openstack_factories.RouterFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            fixed_ips=["10.80.0.13"],
        )
        router.fixed_ips = ["10.80.0.14"]
        router.save()
        floating_ip = self._floating_ip("10.80.0.12")
        floating_ip.refresh_from_db()
        self.assertIsNone(floating_ip.external_address)

    def test_vm_without_resource_is_remapped_through_tenant_offering(self):
        self.instance_resource.delete()
        floating_ip = self._floating_ip("10.80.0.11", self.vm_port)
        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.external_address, "195.80.105.11")

        self.subnet.public_ip_range = "195.80.106.0/24"
        self.subnet.save()
        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.external_address, "195.80.106.11")

    def test_stored_address_is_kept_without_an_offering_to_map_through(self):
        self.tenant_resource.delete()
        self.instance_resource.delete()
        floating_ip = self._floating_ip("10.80.0.12")
        openstack_models.FloatingIP.objects.filter(pk=floating_ip.pk).update(
            external_address="203.0.113.12"
        )
        floating_ip.refresh_from_db()
        floating_ip.name = "renamed"
        floating_ip.port = self._port("192.168.1.60")
        floating_ip.save()

        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.external_address, "203.0.113.12")

    def test_tenant_addresses_are_rebuilt_once_per_transaction(self):
        def real_on_commit(func, using=None, robust=False):
            # conftest runs on_commit callbacks at once; this test needs them
            # queued as in a real transaction.
            transaction.get_connection(using).on_commit(func, robust)

        with (
            mock.patch.object(transaction, "on_commit", real_on_commit),
            mock.patch.object(
                utils,
                "update_tenant_ip_addresses",
                wraps=utils.update_tenant_ip_addresses,
            ) as rebuild,
            self.captureOnCommitCallbacks(execute=True),
        ):
            with transaction.atomic():
                for i in range(5):
                    port = self._port(f"192.168.1.{60 + i}")
                    self._floating_ip(f"10.80.0.{20 + i}", port)
        self.assertEqual(rebuild.call_count, 1)
        self.assertEqual(self._search("195.80.105.24"), {self.tenant_resource.uuid.hex})

    def test_ip_index_names_the_object_each_address_belongs_to(self):
        lb_port = self._port("192.168.1.50", device_owner="Octavia")
        load_balancer = openstack_factories.LoadBalancerFactory(
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.fixture.project,
            name="web-lb",
            vip_address="192.168.1.50",
            vip_port=lb_port,
        )
        lb_ip = self._floating_ip("10.80.0.14", lb_port)
        router = openstack_factories.RouterFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            name="gw",
            fixed_ips=["192.168.1.1", "10.80.0.13"],
        )

        self.tenant_resource.refresh_from_db()
        index = {
            entry["address"]: entry
            for entry in self.tenant_resource.backend_metadata["ip_index"]
        }
        self.assertEqual(
            index["195.80.105.14"],
            {
                "address": "195.80.105.14",
                "type": "floating_ip",
                "uuid": lb_ip.uuid.hex,
                "name": lb_ip.name,
                "mapped_from": "10.80.0.14",
                "attached_to": {"type": "load_balancer", "name": "web-lb"},
            },
        )
        self.assertEqual(
            index["195.80.105.13"],
            {
                "address": "195.80.105.13",
                "type": "router",
                "uuid": router.uuid.hex,
                "name": "gw",
                "mapped_from": "10.80.0.13",
            },
        )
        # The VIP is also a port's fixed IP; the load balancer names it.
        self.assertEqual(index["192.168.1.50"]["type"], "load_balancer")
        self.assertEqual(index["192.168.1.50"]["uuid"], load_balancer.uuid.hex)

    def _index(self):
        self.tenant_resource.refresh_from_db()
        return {
            entry["address"]: entry
            for entry in self.tenant_resource.backend_metadata["ip_index"]
        }

    def test_renames_reach_the_index(self):
        router = openstack_factories.RouterFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            name="gw",
            fixed_ips=["10.80.0.13"],
        )
        floating_ip = self._floating_ip("10.80.0.12")

        router.name = "edge-gw"
        router.save()
        floating_ip.name = "spare"
        floating_ip.save()

        index = self._index()
        self.assertEqual(index["195.80.105.13"]["name"], "edge-gw")
        self.assertEqual(index["195.80.105.12"]["name"], "spare")

    def test_load_balancer_vip_port_change_reaches_the_index(self):
        port = self._port("192.168.1.50", device_owner="Octavia")
        self._floating_ip("10.80.0.14", port)
        load_balancer = openstack_factories.LoadBalancerFactory(
            tenant=self.tenant,
            service_settings=self.settings,
            project=self.fixture.project,
            name="web-lb",
            vip_address="192.168.1.50",
        )
        self.assertEqual(self._index()["195.80.105.14"]["attached_to"]["type"], "port")

        load_balancer.vip_port = port
        load_balancer.save()

        self.assertEqual(
            self._index()["195.80.105.14"]["attached_to"],
            {"type": "load_balancer", "name": "web-lb"},
        )

    def test_releasing_address_clears_mapped_ip_without_an_offering(self):
        self.tenant_resource.delete()
        self.instance_resource.delete()
        floating_ip = self._floating_ip("10.80.0.12")
        openstack_models.FloatingIP.objects.filter(pk=floating_ip.pk).update(
            external_address="195.80.105.12"
        )
        floating_ip.refresh_from_db()

        floating_ip.address = None
        floating_ip.save()

        floating_ip.refresh_from_db()
        self.assertIsNone(floating_ip.external_address)
