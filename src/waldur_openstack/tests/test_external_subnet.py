"""A provider-level external subnet stores its CIDR in full.

Neutron may report an IPv6 prefix written out in full, which is longer than
any IPv4 CIDR; the column has to fit it, as the tenant subnet's does.
"""

from django.test import TestCase

from waldur_openstack import models

from . import factories


class ExternalSubnetCidrTest(TestCase):
    def test_a_fully_written_out_ipv6_cidr_fits(self):
        cidr = "2001:0db8:0000:0000:0000:0000:0000:0000/128"
        self.assertEqual(len(cidr), 43)
        network = factories.ExternalNetworkFactory()

        subnet = models.ExternalSubnet.objects.create(
            network=network, backend_id="external-subnet-1", cidr=cidr, ip_version=6
        )

        subnet.refresh_from_db()
        self.assertEqual(subnet.cidr, cidr)
