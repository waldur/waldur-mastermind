from ddt import data, ddt
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from waldur_mastermind.marketplace.tests import fixtures


@ddt
class RuntimeStatesViewSetTestCase(APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project

    def get_url(self, project=None):
        if project:
            url = 'http://testserver' + reverse(
                'marketplace-runtime-states-list',
                kwargs={'project_uuid': project.uuid.hex},
            )
        else:
            url = 'http://testserver' + reverse('marketplace-runtime-states-list')
        return url

    @data('staff', 'owner', 'admin', 'manager')
    def test_runtime_state_with_project_uuid(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = self.get_url(self.project)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('staff', 'owner', 'admin', 'manager')
    def test_runtime_state_without_project_uuid(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = self.get_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
