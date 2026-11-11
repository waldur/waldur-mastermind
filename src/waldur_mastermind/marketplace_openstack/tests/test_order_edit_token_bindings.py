"""A personal access token's bindings hold for an order edited after creation.

An instance order is checked against the request when it is placed; editing it
while it waits for approval must check it again, or the token's bindings are
lost: execution runs with the user's own API token.
"""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.tests.pat_utils import use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OfferingStates,
    OrderStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.models import Order
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.models import NetworkRBACPolicy
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures as openstack_fixtures

# Seeing the order and editing it.
SCOPES = (PermissionEnum.LIST_ORDERS.value, PermissionEnum.APPROVE_ORDER.value)


@ddt
@override_config(PAT_ENABLED=True)
class InstanceOrderEditTest(test.APITestCase):
    """A user administers projects A and B; B's network is shared into A."""

    def setUp(self):
        self.a = openstack_fixtures.OpenStackFixture()
        self.b = openstack_fixtures.OpenStackFixture()
        self.b.settings = self.a.settings
        self.user = structure_factories.UserFactory()
        self.a.project.add_user(self.user, ProjectRole.ADMIN)
        self.b.project.add_user(self.user, ProjectRole.ADMIN)
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_ORDERS)
        NetworkRBACPolicy.objects.create(
            network=self.b.network,
            target_tenant=self.a.tenant,
            policy_type=NetworkRBACPolicy.NetworkShareType.SHARED,
        )
        self.ssh_key = structure_factories.SshPublicKeyFactory(user=self.user)
        offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.a.tenant,
            state=OfferingStates.ACTIVE,
        )
        self.order = marketplace_factories.OrderFactory(
            offering=offering,
            project=self.a.project,
            created_by=self.user,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.CREATE,
            attributes=self.attributes(),
        )
        # Handlers may move a new order on; it is to wait for approval here.
        Order.objects.filter(pk=self.order.pk).update(
            state=OrderStates.PENDING_CONSUMER
        )

    def attributes(self, **port):
        return {
            "flavor": openstack_factories.FlavorFactory.get_url(self.a.flavor),
            "image": openstack_factories.ImageFactory.get_url(self.a.image),
            "name": "virtual-machine",
            "system_volume_size": self.a.image.min_disk,
            "ports": [
                {"subnet": openstack_factories.SubNetFactory.get_url(self.b.subnet)}
                | port
            ],
            "ssh_public_key": structure_factories.SshPublicKeyFactory.get_url(
                self.ssh_key
            ),
        }

    def pin_an_address_on_bs_network(self):
        pinned = self.attributes(
            fixed_ips=[
                {"ip_address": "192.168.42.100", "subnet_id": self.b.subnet.backend_id}
            ]
        )
        return self.client.patch(
            marketplace_factories.OrderFactory.get_url(self.order),
            {"attributes": pinned},
            format="json",
        )

    # A restore order runs through the create processor too.
    @data(OrderTypes.CREATE, OrderTypes.RESTORE)
    def test_a_token_bound_to_the_order_project_cannot(self, order_type):
        Order.objects.filter(pk=self.order.pk).update(type=order_type)
        use_pat(self.client, self.user, *SCOPES, bindings=[self.a.project])

        response = self.pin_an_address_on_bs_network()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertNotIn("fixed_ips", self.order.attributes["ports"][0])

    @data(OrderTypes.CREATE, OrderTypes.RESTORE)
    def test_an_unbound_token_of_the_networks_owner_can(self, order_type):
        Order.objects.filter(pk=self.order.pk).update(type=order_type)
        use_pat(self.client, self.user, *SCOPES)

        response = self.pin_an_address_on_bs_network()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
