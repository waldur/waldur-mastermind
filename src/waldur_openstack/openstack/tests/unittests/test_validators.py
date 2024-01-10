import unittest

import ddt
from django.core.exceptions import ValidationError

from waldur_openstack.openstack.serializers import validate_private_subnet_cidr


@ddt.ddt
class PrivateSubnetCIDRTest(unittest.TestCase):
    @ddt.data("192.168.42.0/24", "172.19.200.0/24", "10.10.10.0/24")
    def test_positive(self, cidr):
        # it is expected that exception is not thrown here
        validate_private_subnet_cidr(cidr)

    @ddt.data("127.0.0.0/24", "8.8.8.8/24", "216.58.211.0/24")
    def test_negative(self, cidr):
        self.assertRaises(ValidationError, validate_private_subnet_cidr, cidr)
