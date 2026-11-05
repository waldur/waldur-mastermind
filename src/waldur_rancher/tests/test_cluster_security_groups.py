from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_openstack.tests import factories as openstack_factories
from waldur_rancher import models

from . import factories, fixtures


@ddt
@mock.patch("waldur_rancher.views.PushSecurityGroupRulesExecutor")
class ClusterSecurityGroupUpdateTest(test.APITestCase):
    """Setting a cluster's security group rules takes what changing the cluster takes."""

    def setUp(self):
        self.fixture = fixtures.RancherFixture()
        self.group = factories.ClusterSecurityGroupFactory(cluster=self.fixture.cluster)
        self.rule = models.ClusterSecurityGroupRule.objects.create(
            group=self.group,
            ethertype="IPv4",
            direction="ingress",
            protocol="tcp",
            from_port=443,
            to_port=443,
            cidr="10.0.0.0/24",
        )
        # The OpenStack group the rules are pushed to, in the node's tenant.
        openstack_factories.SecurityGroupFactory(
            name=self.group.name, tenant=self.fixture.tenant
        )
        self.url = factories.ClusterSecurityGroupFactory.get_url(self.group)

    @data("staff", "owner", "admin", "manager")
    def test_who_may_change_the_cluster_can_set_the_rules(self, user, executor):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url, {"rules": []}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(self.group.rules.exists())

    @data("member", "customer_support", "global_support")
    def test_who_only_sees_the_cluster_cannot_set_the_rules(self, user, executor):
        self.client.force_authenticate(getattr(self.fixture, user))
        for method in (self.client.patch, self.client.put):
            response = method(self.url, {"rules": []}, format="json")
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(self.group.rules.filter(pk=self.rule.pk).exists())
        executor.return_value.execute.assert_not_called()

    @data("member", "global_support")
    def test_who_sees_the_cluster_can_still_read_the_rules(self, user, executor):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["rules"]), 1)
