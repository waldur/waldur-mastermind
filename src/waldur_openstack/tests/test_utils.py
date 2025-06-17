from django.test import TestCase

from waldur_openstack.tests.factories import (
    SecurityGroupFactory,
    SecurityGroupRuleFactory,
)
from waldur_openstack.utils import reorder_security_groups_topologically


class TestSecurityGroupTopologicalSort(TestCase):
    def setUp(self):
        self.sg1 = SecurityGroupFactory(name="sg1")
        self.sg2 = SecurityGroupFactory(name="sg2")
        self.sg3 = SecurityGroupFactory(name="sg3")

    def test_reorder_security_groups_with_no_dependencies(self):
        groups = [self.sg1, self.sg2]
        ordered = reorder_security_groups_topologically(groups)
        self.assertListEqual(ordered, groups)

    def test_reorder_security_groups_with_dependencies(self):
        SecurityGroupRuleFactory(security_group=self.sg1, remote_group=self.sg2)
        SecurityGroupRuleFactory(security_group=self.sg2, remote_group=self.sg3)
        ordered = reorder_security_groups_topologically([self.sg1, self.sg2, self.sg3])
        self.assertListEqual(ordered, [self.sg3, self.sg2, self.sg1])
