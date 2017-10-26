from ddt import data, ddt
from rest_framework import test, status

from nodeconductor.structure.tests import fixtures, factories as structure_factories

from nodeconductor_assembly_waldur.experts import models

from . import factories


@ddt
class ExpertProviderRegisterTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_register_an_expert_provider(self, user):
        response = self.create_expert_provider(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.ExpertProvider.objects.filter(customer=self.customer).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_register_an_expert_provider(self, user):
        response = self.create_expert_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('staff', 'owner')
    def test_authorized_user_can_update_expert_provider(self, user):
        response, expert_provider = self.update_expert_provider(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(expert_provider.enable_notifications)
        self.assertTrue(models.ExpertProvider.objects.filter(customer=self.customer).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_expert_provider(self, user):
        response, expert_provider = self.update_expert_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_expert_provider(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ExpertProviderFactory.get_list_url()

        payload = {
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
            'agree_with_policy': True
        }

        return self.client.post(url, payload)

    def update_expert_provider(self, user):
        expert_provider = factories.ExpertProviderFactory(customer=self.customer)
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ExpertProviderFactory.get_url(expert_provider)

        response = self.client.patch(url, {
            'enable_notifications': False
        })
        expert_provider.refresh_from_db()

        return response, expert_provider

    def test_user_cannot_register_an_expert_provider_if_does_not_agree_with_policies(self):
        self.client.force_authenticate(self.fixture.staff)
        customer = self.fixture.customer
        url = factories.ExpertProviderFactory.get_list_url()

        payload = {
            'customer': structure_factories.CustomerFactory.get_url(customer),
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('agree_with_policy', response.data)
