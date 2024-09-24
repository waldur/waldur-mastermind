from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.tests import factories as openstack_factories


class NetworkMtuTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.tenant = openstack_factories.TenantFactory()
        self.resource = marketplace_factories.ResourceFactory(scope=self.tenant)
        self.offering = self.resource.offering

    def test_set_mtu(self):
        network = openstack_factories.NetworkFactory(tenant=self.tenant)
        network.refresh_from_db()
        self.assertEqual(network.mtu, None)

        self.offering.plugin_options["default_internal_network_mtu"] = 1000
        self.offering.save()

        network = openstack_factories.NetworkFactory(tenant=self.tenant)
        network.refresh_from_db()
        self.assertEqual(network.mtu, 1000)
