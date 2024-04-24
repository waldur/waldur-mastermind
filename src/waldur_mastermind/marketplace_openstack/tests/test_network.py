from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.openstack.tests import factories as openstack_factories


class NetworkMtuTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.resource = marketplace_factories.ResourceFactory()
        self.offering = self.resource.offering
        self.tenant = openstack_factories.TenantFactory()
        self.resource.scope = self.tenant
        self.resource.save()

    def test_set_mtu(self):
        network = openstack_factories.NetworkFactory(tenant=self.tenant)
        network.refresh_from_db()
        self.assertEqual(network.mtu, None)

        self.offering.attributes["default_internal_network_mtu"] = 1000
        self.offering.save()

        network = openstack_factories.NetworkFactory(tenant=self.tenant)
        network.refresh_from_db()
        self.assertEqual(network.mtu, 1000)
