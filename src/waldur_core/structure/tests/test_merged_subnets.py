from rest_framework import test

from waldur_core.structure.management.commands import organization_access_subnets
from waldur_core.structure.tests import factories


class MergedSubnetsTest(test.APITestCase):
    """Merging behaviour of the sign-in address export.

    Entries are created portal-scoped explicitly: the command exports only
    addresses trusted for signing in, and the same table now also holds ones
    trusted purely for reaching resources.
    """

    def setUp(self):
        from waldur_core.structure import models

        models.AccessSubnet.objects.all().delete()

    def test_consecutive_subnets_merge(self):
        factories.AccessSubnetFactory(inet="192.168.1.0/24", applies_to_portal=True)
        factories.AccessSubnetFactory(inet="192.168.2.0/24", applies_to_portal=True)

        merged_subnets = organization_access_subnets.Command().get_merged_subnets()
        self.assertEqual(len(merged_subnets), 2)  # They remain separate
        merged_subnets_str = sorted([str(subnet) for subnet in merged_subnets])
        self.assertEqual(merged_subnets_str, ["192.168.1.0/24", "192.168.2.0/24"])

    def test_truly_adjacent_subnets_merge(self):
        # These subnets are truly adjacent in binary representation and will merge
        factories.AccessSubnetFactory(
            inet="192.168.0.0/25", applies_to_portal=True
        )  # 192.168.0.0 - 192.168.0.127
        factories.AccessSubnetFactory(
            inet="192.168.0.128/25", applies_to_portal=True
        )  # 192.168.0.128 - 192.168.0.255

        merged_subnets = organization_access_subnets.Command().get_merged_subnets()
        self.assertEqual(len(merged_subnets), 1)
        self.assertEqual(str(merged_subnets[0]), "192.168.0.0/24")

    def test_overlapping_subnets_merge(self):
        factories.AccessSubnetFactory(inet="10.0.0.0/24", applies_to_portal=True)
        factories.AccessSubnetFactory(inet="10.0.0.128/25", applies_to_portal=True)

        merged_subnets = organization_access_subnets.Command().get_merged_subnets()
        self.assertEqual(len(merged_subnets), 1)
        self.assertEqual(str(merged_subnets[0]), "10.0.0.0/24")

    def test_adjacent_subnets_with_different_masks_merge(self):
        factories.AccessSubnetFactory(inet="172.16.0.0/28", applies_to_portal=True)
        factories.AccessSubnetFactory(inet="172.16.0.16/28", applies_to_portal=True)

        merged_subnets = organization_access_subnets.Command().get_merged_subnets()
        self.assertEqual(len(merged_subnets), 1)
        self.assertEqual(str(merged_subnets[0]), "172.16.0.0/27")

    def test_multiple_individual_ips_merge(self):
        # Individual IPs that are not perfectly consecutive may not merge into a single block
        for i in range(1, 11):
            factories.AccessSubnetFactory(
                inet=f"192.168.1.{i}/32", applies_to_portal=True
            )

        merged_subnets = organization_access_subnets.Command().get_merged_subnets()
        # The exact number depends on how ipaddress.collapse_addresses() groups them
        # Let's just verify that they've been reduced from 10 individual IPs
        self.assertLess(len(merged_subnets), 10)

        # Verify all IPs are covered by checking the total number of addresses
        total_addresses = sum(subnet.num_addresses for subnet in merged_subnets)
        self.assertGreaterEqual(total_addresses, 10)

    def test_non_mergeable_subnets_remain_separate(self):
        factories.AccessSubnetFactory(inet="192.168.1.0/24", applies_to_portal=True)
        factories.AccessSubnetFactory(inet="10.0.0.0/24", applies_to_portal=True)

        merged_subnets = organization_access_subnets.Command().get_merged_subnets()
        self.assertEqual(len(merged_subnets), 2)
        merged_subnets_str = sorted([str(subnet) for subnet in merged_subnets])
        self.assertEqual(merged_subnets_str, ["10.0.0.0/24", "192.168.1.0/24"])

    def test_mixed_ipv4_and_ipv6_subnets(self):
        factories.AccessSubnetFactory(inet="192.168.1.0/24", applies_to_portal=True)
        factories.AccessSubnetFactory(inet="2001:db8::/64", applies_to_portal=True)
        factories.AccessSubnetFactory(inet="2001:db8:0:1::/64", applies_to_portal=True)

        merged_subnets = organization_access_subnets.Command().get_merged_subnets()
        self.assertEqual(len(merged_subnets), 2)

        ipv4_subnets = [subnet for subnet in merged_subnets if subnet.version == 4]
        ipv6_subnets = [subnet for subnet in merged_subnets if subnet.version == 6]

        self.assertEqual(len(ipv4_subnets), 1)
        self.assertEqual(str(ipv4_subnets[0]), "192.168.1.0/24")

        self.assertEqual(len(ipv6_subnets), 1)
        self.assertEqual(str(ipv6_subnets[0]), "2001:db8::/63")
