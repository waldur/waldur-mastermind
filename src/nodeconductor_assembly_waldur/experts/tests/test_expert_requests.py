from rest_framework import test, status

from nodeconductor.structure.tests import factories as structure_factories
from nodeconductor.structure.tests import fixtures as structure_fixtures

from .. import models
from . import factories


class ExpertRequestTest(test.APITransactionTestCase):
    def setUp(self):
        self.project_fixture = structure_fixtures.ProjectFixture()
        self.project_manager = self.project_fixture.manager
        self.customer_owner = self.project_fixture.owner
        self.project = self.project_fixture.project

        self.expert_fixture = structure_fixtures.ProjectFixture()
        self.expert_provider = factories.ExpertProviderFactory(customer=self.expert_fixture.customer)
        self.expert_manager = self.expert_fixture.owner

    def test_expert_requests_are_visible_to_any_expert_manager(self):
        self.client.force_authenticate(self.expert_manager)
        self.expert_request = factories.ExpertRequestFactory(project=self.project)
        url = factories.ExpertRequestFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(1, len(response.data))

    def test_expert_requests_are_not_visible_to_project_manager(self):
        self.client.force_authenticate(self.project_manager)
        self.expert_request = factories.ExpertRequestFactory(project=self.project)
        url = factories.ExpertRequestFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(0, len(response.data))

    def test_expert_request_could_be_created_by_customer_owner(self):
        self.client.force_authenticate(self.customer_owner)
        url = factories.ExpertRequestFactory.get_list_url()
        response = self.client.post(url, {
            'project': structure_factories.ProjectFactory.get_url(self.project)
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.ExpertRequest.objects.filter(project=self.project).exists())

    def test_expert_request_could_not_be_created_by_project_manager(self):
        self.client.force_authenticate(self.project_manager)
        url = factories.ExpertRequestFactory.get_list_url()
        response = self.client.post(url, {
            'project': structure_factories.ProjectFactory.get_url(self.project)
        })
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.ExpertRequest.objects.filter(project=self.project).exists())

    def test_expert_request_could_be_created_for_project_without_active_request(self):
        self.expert_request = factories.ExpertRequestFactory(project=self.project)
        self.client.force_authenticate(self.customer_owner)
        url = factories.ExpertRequestFactory.get_list_url()
        response = self.client.post(url, {
            'project': structure_factories.ProjectFactory.get_url(self.project)
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.ExpertRequest.objects.filter(project=self.project).exists())

    def test_expert_request_could_not_be_created_for_project_with_active_request(self):
        self.expert_request = factories.ExpertRequestFactory(
            project=self.project, state=models.ExpertRequest.States.ACTIVE)
        self.client.force_authenticate(self.customer_owner)
        url = factories.ExpertRequestFactory.get_list_url()
        response = self.client.post(url, {
            'project': structure_factories.ProjectFactory.get_url(self.project)
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(models.ExpertRequest.objects.filter(project=self.project).exists())
