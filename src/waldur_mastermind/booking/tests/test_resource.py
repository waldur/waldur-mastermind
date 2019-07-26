from rest_framework import test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class OrderItemProcessedTest(test.APITransactionTestCase):

    def test_get_resource_list(self):
        fixture_1 = structure_fixtures.CustomerFixture()
        fixture_1.owner
        fixture_2 = structure_fixtures.CustomerFixture()
        fixture_2.owner
        offering_1 = marketplace_factories.OfferingFactory(customer=fixture_1.customer)
        offering_2 = marketplace_factories.OfferingFactory(customer=fixture_2.customer)
        resource_1 = marketplace_factories.ResourceFactory(offering=offering_1)
        marketplace_factories.ResourceFactory(offering=offering_2)
        owner = resource_1.offering.customer.get_owners()[0]
        self.client.force_authenticate(owner)
        url = reverse('booking-resource-list')
        response = self.client.get(url)
        response
