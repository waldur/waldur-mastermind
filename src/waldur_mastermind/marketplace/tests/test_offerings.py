from ddt import data, ddt
from rest_framework import test, status

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models

from . import factories


@ddt
class OfferingGetTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(attributes='')

    @data('staff', 'owner', 'user', 'customer_support', 'admin', 'manager')
    def test_offerings_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_offerings_should_be_invisible_to_unauthenticated_users(self):
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class OfferingCreateTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_create_offering(self, user):
        response = self.create_offering(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Offering.objects.filter(provider__customer=self.customer).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_create_offering(self, user):
        response = self.create_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        payload = {
            'name': 'offering',
            'attributes': '',
            'category': factories.CategoryFactory.get_url(),
            'provider': factories.ServiceProviderFactory.get_url(self.provider),
        }

        return self.client.post(url, payload)


@ddt
class OfferingUpdateTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_update_offering(self, user):
        response, offering = self.update_offering(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.name, 'new_offering')
        self.assertTrue(models.Offering.objects.filter(name='new_offering').exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_offering(self, user):
        response, offering = self.update_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        provider = factories.ServiceProviderFactory(customer=self.customer)
        offering = factories.OfferingFactory(provider=provider, attributes='')
        url = factories.OfferingFactory.get_url(offering)

        response = self.client.patch(url, {
            'name': 'new_offering'
        })
        offering.refresh_from_db()

        return response, offering


@ddt
class OfferingDeleteTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(provider=self.provider, attributes='')

    @data('staff', 'owner')
    def test_authorized_user_can_delete_offering(self, user):
        response = self.delete_offering(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT, response.data)
        self.assertFalse(models.Offering.objects.filter(provider__customer=self.customer).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_delete_offering(self, user):
        response = self.delete_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Offering.objects.filter(provider__customer=self.customer).exists())

    def delete_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.delete(url)
        return response
