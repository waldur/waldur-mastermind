from django.core.exceptions import ValidationError
from django.test import TestCase

from waldur_core.core.enums import CoreStates
from waldur_openstack.admin import TenantAdmin

from . import factories, fixtures


class AllocateFloatingIPAdminActionTest(TestCase):
    """The admin action refuses what Neutron would refuse afterwards, as the
    API does since the same check was added there."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.tenant.state = CoreStates.OK
        self.tenant.save()
        self.action = TenantAdmin.AllocateFloatingIP()

    def _use_external_network(self, *ip_versions):
        network = factories.ExternalNetworkFactory(settings=self.fixture.settings)
        for ip_version in ip_versions:
            if ip_version == 6:
                factories.ExternalSubnetFactory(
                    network=network,
                    ip_version=6,
                    cidr="2001:db8::/64",
                    gateway_ip="2001:db8::1",
                )
            else:
                factories.ExternalSubnetFactory(network=network, ip_version=4)
        self.tenant.external_network_ref = network
        self.tenant.external_network_id = network.backend_id
        self.tenant.save()
        return network

    def test_an_ipv6_only_external_network_is_refused(self):
        self._use_external_network(6)

        with self.assertRaises(ValidationError) as raised:
            self.action.validate(self.tenant)

        self.assertIn("no IPv4 subnet", str(raised.exception))

    def test_an_ipv4_external_network_is_allowed(self):
        self._use_external_network(4)

        self.action.validate(self.tenant)

    def test_a_dual_stack_external_network_is_allowed(self):
        self._use_external_network(4, 6)

        self.action.validate(self.tenant)

    def test_a_network_whose_subnets_are_unknown_is_allowed(self):
        self._use_external_network()

        self.action.validate(self.tenant)

    def test_a_tenant_without_an_external_network_is_still_refused(self):
        self.tenant.external_network_ref = None
        self.tenant.external_network_id = ""
        self.tenant.save()

        with self.assertRaises(ValidationError) as raised:
            self.action.validate(self.tenant)

        self.assertIn("external network", str(raised.exception))
