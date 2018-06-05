from ddt import data, ddt
from rest_framework import test, status

from waldur_core.structure.tests import fixtures, factories as structure_factories

from waldur_mastermind.marketplace import models

from . import factories


@ddt
class ServiceProviderGetTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.service_provider = factories.ServiceProviderFactory()

    @data('staff', 'owner', 'user', 'customer_support', 'admin', 'manager')
    def test_service_provider_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_service_provider_should_be_invisible_to_unauthenticated_users(self):
        url = factories.ServiceProviderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class ServiceProviderRegisterTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_register_an_service_provider(self, user):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.ServiceProvider.objects.filter(customer=self.customer).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_register_an_service_provider(self, user):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_service_provider(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_list_url()

        payload = {
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
        }

        return self.client.post(url, payload)


@ddt
class ServiceProviderUpdateTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_update_service_provider(self, user):
        response, service_provider = self.update_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(service_provider.enable_notifications)
        self.assertTrue(models.ServiceProvider.objects.filter(customer=self.customer).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_service_provider(self, user):
        response, service_provider = self.update_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_service_provider(self, user):
        service_provider = factories.ServiceProviderFactory(customer=self.customer)
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_url(service_provider)

        response = self.client.patch(url, {
            'enable_notifications': False
        })
        service_provider.refresh_from_db()

        return response, service_provider


@ddt
class ServiceProviderDeleteTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)

    @data('staff', 'owner')
    def test_authorized_user_can_delete_service_provider(self, user):
        response = self.delete_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT, response.data)
        self.assertFalse(models.ServiceProvider.objects.filter(customer=self.customer).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_delete_service_provider(self, user):
        response = self.delete_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.ServiceProvider.objects.filter(customer=self.customer).exists())

    def delete_service_provider(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_url(self.service_provider)
        response = self.client.delete(url)
        return response
