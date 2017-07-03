from ddt import data, ddt
from rest_framework import test, status

from nodeconductor.structure.tests import fixtures, factories as structure_factories

from nodeconductor_assembly_waldur.experts import models

from . import factories


@ddt
class ExpertProviderRegisterTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.CustomerFixture()

    @data('staff', 'owner')
    def test_user_can_register_an_expert_provider(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        customer = self.fixture.customer
        url = factories.ExpertProviderFactory.get_list_url()

        payload = {
            'customer': structure_factories.CustomerFactory.get_url(customer),
            'agree_with_policy': True
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.ExpertProvider.objects.filter(customer=customer).exists())

    @data('user', 'customer_support')
    def test_user_has_no_permissions_to_register_an_expert_provider(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        customer = self.fixture.customer
        url = factories.ExpertProviderFactory.get_list_url()

        payload = {
            'customer': structure_factories.CustomerFactory.get_url(customer),
            'agree_with_policy': True
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

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
