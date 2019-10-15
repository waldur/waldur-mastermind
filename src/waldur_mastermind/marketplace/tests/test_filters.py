from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories


class CustomerResourcesFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture1 = structure_fixtures.ServiceFixture()
        self.customer1 = self.fixture1.customer
        self.offering = factories.OfferingFactory(customer=self.customer1)
        self.resource1 = factories.ResourceFactory(offering=self.offering, project=self.fixture1.project)

        self.fixture2 = structure_fixtures.ServiceFixture()
        self.customer2 = self.fixture2.customer

    def list_customers(self, has_resources):
        list_url = structure_factories.CustomerFactory.get_list_url()
        self.client.force_authenticate(self.fixture1.staff)
        if has_resources:
            return self.client.get(list_url, {'has_resources': has_resources}).data
        else:
            return self.client.get(list_url).data

    def test_list_customers_with_resources(self):
        self.assertEqual(1, len(self.list_customers(True)))

    def test_list_all_customers(self):
        self.assertEqual(2, len(self.list_customers(False)))
