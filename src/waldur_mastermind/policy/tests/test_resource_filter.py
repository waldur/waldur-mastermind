"""Filtering project cost policies by the resource they are scoped to.

A policy with `resource` set measures only that resource's invoice items, so a
resource-oriented view needs to ask for exactly those, and a project-wide view
needs to exclude them rather than plot them against project-wide cost.
"""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy.tests import factories


@ddt
class ProjectEstimatedCostPolicyResourceFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_factories.ProjectFactory()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer
        )
        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture, offering=self.offering
        )
        self.other_resource = marketplace_factories.ResourceFactory(
            project=self.fixture, offering=self.offering
        )
        self.scoped = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.fixture, resource=self.resource
        )
        self.other_scoped = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.fixture, resource=self.other_resource
        )
        self.project_wide = factories.ProjectEstimatedCostPolicyFactory(
            scope=self.fixture
        )
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)
        self.url = factories.ProjectEstimatedCostPolicyFactory.get_list_url()

    def uuids(self, response):
        return {item["uuid"] for item in response.data}

    def test_filter_by_resource_uuid_returns_only_that_resources_policy(self):
        response = self.client.get(self.url, {"resource_uuid": self.resource.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.uuids(response), {self.scoped.uuid.hex})

    def test_project_wide_policy_is_not_returned_for_a_resource(self):
        # It measures the whole project, so it is deliberately absent — that is
        # the distinction the filter exists to make.
        response = self.client.get(self.url, {"resource_uuid": self.resource.uuid.hex})
        self.assertNotIn(self.project_wide.uuid.hex, self.uuids(response))

    @data(True, False)
    def test_has_resource_partitions_the_project_policies(self, has_resource):
        response = self.client.get(
            self.url,
            {"project_uuid": self.fixture.uuid.hex, "has_resource": has_resource},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected = (
            {self.scoped.uuid.hex, self.other_scoped.uuid.hex}
            if has_resource
            else {self.project_wide.uuid.hex}
        )
        self.assertEqual(self.uuids(response), expected)

    def test_unfiltered_list_still_returns_every_policy(self):
        response = self.client.get(self.url, {"project_uuid": self.fixture.uuid.hex})
        self.assertEqual(
            self.uuids(response),
            {
                self.scoped.uuid.hex,
                self.other_scoped.uuid.hex,
                self.project_wide.uuid.hex,
            },
        )

    def test_filter_by_resource_url(self):
        url = marketplace_factories.ResourceFactory.get_url(self.resource)
        response = self.client.get(self.url, {"resource": url})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.uuids(response), {self.scoped.uuid.hex})
