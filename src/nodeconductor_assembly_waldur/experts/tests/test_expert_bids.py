from rest_framework import test, status

from nodeconductor.structure.tests import factories as structure_factories
from nodeconductor.structure.tests import fixtures as structure_fixtures

from . import factories


class ExpertBidTest(test.APITransactionTestCase):
    def setUp(self):
        self.expert_fixture = structure_fixtures.ProjectFixture()
        self.expert_manager = self.expert_fixture.owner
        self.expert_provider = factories.ExpertProviderFactory(customer=self.expert_fixture.customer)

        self.project_fixture = structure_fixtures.ProjectFixture()
        self.project = self.project_fixture.project
        self.expert_request = factories.ExpertRequestFactory(project=self.project)

    def test_expert_manager_can_create_expert_bid(self):
        self.client.force_authenticate(self.expert_manager)
        response = self.create_expert_bid()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_project_manager_can_not_create_expert_bid(self):
        self.client.force_authenticate(self.project_fixture.manager)
        response = self.create_expert_bid()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_other_expert_manager_can_not_see_expert_bid(self):
        expert_bid = factories.ExpertBidFactory(request=self.expert_request, team=self.project)
        fixture = structure_fixtures.ProjectFixture()
        factories.ExpertProviderFactory(customer=fixture.customer)
        self.client.force_authenticate(fixture.owner)
        response = self.client.get(factories.ExpertBidFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def create_expert_bid(self):
        url = factories.ExpertBidFactory.get_list_url()
        return self.client.post(url, {
            'request': factories.ExpertRequestFactory.get_url(self.expert_request),
            'team': structure_factories.ProjectFactory.get_url(self.project),
            'price': 100.00,
        })
