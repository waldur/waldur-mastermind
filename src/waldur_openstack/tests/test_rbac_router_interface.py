"""Routing a subnet shared over RBAC from the consumer's own router (#394).

A network shared with `access_as_shared` is routed by the tenant that *consumes*
it: Neutron accepts the attachment and puts the `network:router_interface` port
in the router's project, not the subnet owner's. Waldur refused it outright --
`OpenStackRouterInterfaceSerializer` compared `subnet.tenant` with the router's
-- so the case #388 taught `SubNet.router` to represent could not be reached
through the API or Homeport at all.

The check is now visibility (`Tenant.available_subnets`), and the port the action
creates is owned by the tenant whose router holds it, which is where Neutron puts
it and where its quota and billing belong.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_openstack import models

from . import factories, fixtures


class SharedSubnetRouterInterfaceTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.owner)

        # The owner's network and subnet, which the consumer will route.
        self.owner_tenant = self.fixture.tenant
        self.network = self.fixture.network
        self.subnet = self.fixture.subnet

        # A second tenant in the same project, with a router of its own.
        self.consumer_tenant = factories.TenantFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            state=CoreStates.OK,
            backend_id="consumer-tenant-backend-id",
        )
        self.consumer_router = factories.RouterFactory(
            tenant=self.consumer_tenant,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            state=CoreStates.OK,
            backend_id="consumer-router-backend-id",
        )

        self.url_add = factories.RouterFactory.get_url(
            self.consumer_router, action="add_router_interface"
        )
        self.url_remove = factories.RouterFactory.get_url(
            self.consumer_router, action="remove_router_interface"
        )
        self.payload = {"subnet": factories.SubNetFactory.get_url(self.subnet)}

        patcher = mock.patch(
            "waldur_openstack.backend.OpenStackBackend.get_free_ip",
            return_value="192.168.42.10",
        )
        self.mock_get_free_ip = patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("create_port", "add_router_interface", "pull_tenant_routers"):
            patcher = mock.patch(f"waldur_openstack.backend.OpenStackBackend.{name}")
            setattr(self, f"mock_{name}", patcher.start())
            self.addCleanup(patcher.stop)

    def share(self):
        return factories.NetworkRBACPolicyFactory(
            network=self.network, target_tenant=self.consumer_tenant
        )

    def test_a_shared_subnet_can_be_attached_to_the_consumers_router(self):
        self.share()

        response = self.client.post(self.url_add, self.payload)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.mock_add_router_interface.assert_called_once()

    def test_without_a_policy_the_subnet_is_still_refused(self):
        response = self.client.post(self.url_add, self.payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("shared", str(response.data))
        self.mock_add_router_interface.assert_not_called()

    def test_a_policy_naming_another_tenant_does_not_help(self):
        """Visibility is per tenant: a share with someone else is not a share
        with this router's tenant."""
        third_tenant = factories.TenantFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            state=CoreStates.OK,
        )
        factories.NetworkRBACPolicyFactory(
            network=self.network, target_tenant=third_tenant
        )

        response = self.client.post(self.url_add, self.payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_interface_port_belongs_to_the_routers_tenant(self):
        """Neutron puts it in the router's project, so the row has to agree --
        otherwise the interface is billed to the tenant that shared the network
        and counts against its quota."""
        self.share()

        self.client.post(self.url_add, self.payload)

        port = models.Port.objects.get(subnet=self.subnet)
        self.assertEqual(port.tenant, self.consumer_tenant)
        self.assertEqual(port.project, self.consumer_router.project)
        self.assertEqual(port.service_settings, self.subnet.service_settings)
        self.assertEqual(port.network, self.network)

    def test_the_subnet_is_reported_as_connected_at_once(self):
        """`pull_subnets` is the only other writer of the flag and runs every two
        hours; until then the tab would read "<router> (disconnected)"."""
        self.share()
        self.subnet.is_connected = False
        self.subnet.save()

        self.client.post(self.url_add, self.payload)

        self.subnet.refresh_from_db()
        self.assertTrue(self.subnet.is_connected)

    def test_removal_is_allowed_for_a_shared_subnet_too(self):
        """One serializer backs both actions, so a consumer that can attach can
        detach as well."""
        self.share()

        with mock.patch(
            "waldur_openstack.executors.RouterInterfaceDeleteExecutor.execute"
        ) as executor:
            response = self.client.post(self.url_remove, self.payload)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        executor.assert_called_once()

    def test_removal_without_a_policy_is_refused(self):
        with mock.patch(
            "waldur_openstack.executors.RouterInterfaceDeleteExecutor.execute"
        ) as executor:
            response = self.client.post(self.url_remove, self.payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        executor.assert_not_called()


class OwnSubnetRouterInterfaceTest(test.APITestCase):
    """The unshared path, which the visibility check must leave alone."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.owner)
        self.router = self.fixture.router
        self.subnet = self.fixture.subnet
        self.url = factories.RouterFactory.get_url(
            self.router, action="add_router_interface"
        )
        patcher = mock.patch(
            "waldur_openstack.backend.OpenStackBackend.get_free_ip",
            return_value="192.168.42.10",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("create_port", "add_router_interface", "pull_tenant_routers"):
            patcher = mock.patch(f"waldur_openstack.backend.OpenStackBackend.{name}")
            setattr(self, f"mock_{name}", patcher.start())
            self.addCleanup(patcher.stop)

    def test_an_own_subnet_needs_no_policy(self):
        response = self.client.post(
            self.url, {"subnet": factories.SubNetFactory.get_url(self.subnet)}
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    def test_the_port_is_owned_as_before(self):
        self.client.post(
            self.url, {"subnet": factories.SubNetFactory.get_url(self.subnet)}
        )

        port = models.Port.objects.get(subnet=self.subnet)
        self.assertEqual(port.tenant, self.subnet.tenant)
        self.assertEqual(port.project, self.subnet.project)
        self.assertEqual(port.service_settings, self.subnet.service_settings)

    def test_a_subnet_of_a_foreign_tenant_is_refused(self):
        other_tenant = factories.TenantFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            state=CoreStates.OK,
        )
        other_network = factories.NetworkFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=other_tenant,
            state=CoreStates.OK,
        )
        other_subnet = factories.SubNetFactory(
            network=other_network,
            tenant=other_tenant,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            state=CoreStates.OK,
        )

        response = self.client.post(
            self.url, {"subnet": factories.SubNetFactory.get_url(other_subnet)}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
