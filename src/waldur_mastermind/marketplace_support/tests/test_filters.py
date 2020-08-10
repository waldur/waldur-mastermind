from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support.tests import factories as support_factories


class ResourceBackendIDFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.UserFixture()
        self.resource = marketplace_factories.ResourceFactory()
        marketplace_factories.ResourceFactory()
        self.request = support_factories.OfferingFactory(
            backend_id='offering_backend_id'
        )
        self.resource.scope = self.request
        self.resource.save()
        self.url = marketplace_factories.ResourceFactory.get_list_url()

    def test_backend_id_filter(self):
        self.client.force_login(self.fixture.staff)
        response = self.client.get(self.url, {'backend_id': 'offering_backend_id'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
