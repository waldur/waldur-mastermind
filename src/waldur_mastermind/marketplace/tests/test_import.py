from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import factories


@ddt
class ImportableOfferingsListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()

    def list_offerings(self, shared, user):
        factories.OfferingFactory(
            scope=self.fixture.service_settings,
            shared=shared,
            customer=self.fixture.customer,
        )
        list_url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.get(list_url, {'importable': True}).data

    def test_staff_can_list_importable_shared_offerings(self):
        offerings = self.list_offerings(shared=True, user='staff')
        self.assertEqual(1, len(offerings))

    @data('owner', 'manager', 'admin')
    def test_other_users_can_not_list_importable_shared_offerings(self, user):
        offerings = self.list_offerings(shared=True, user=user)
        self.assertEqual(0, len(offerings))

    @data('staff', 'owner')
    def test_staff_and_owner_can_list_importable_private_offerings(self, user):
        offerings = self.list_offerings(shared=False, user=user)
        self.assertEqual(1, len(offerings))


class ImportableResourcesListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()

    def list_resources(self, shared, user):
        offering = factories.OfferingFactory(
            scope=self.fixture.service_settings,
            shared=shared,
            customer=self.fixture.customer,
        )
        list_url = factories.OfferingFactory.get_url(offering, 'importable_resources')
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.get(list_url)

    def test_staff_can_list_importable_resources_from_shared_offering(self):
        response = self.list_resources(shared=True, user='staff')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_can_not_list_importable_resources_from_shared_offering(self):
        response = self.list_resources(shared=True, user='owner')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_list_importable_resources_from_private_offering(self):
        response = self.list_resources(shared=False, user='owner')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_manager_cannot_list_importable_resources(self):
        response = self.list_resources(shared=False, user='manager')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_another_owner_can_list_importable_resources(self):
        offering = factories.OfferingFactory(
            scope=self.fixture.service_settings, shared=False,
        )
        offering.allowed_customers.set([self.fixture.customer])
        list_url = factories.OfferingFactory.get_url(offering, 'importable_resources')
        self.client.force_authenticate(getattr(self.fixture, 'owner'))
        response = self.client.get(list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
