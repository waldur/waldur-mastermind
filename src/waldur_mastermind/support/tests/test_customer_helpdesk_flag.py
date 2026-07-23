from constance.test.unittest import override_config
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support.tests import factories


@override_config(WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED=True)
class CustomerHasActiveHelpdeskFlagTest(test.APITestCase):
    """The customer serializer exposes ``has_active_helpdesk`` so the UI can hide
    the provider Helpdesk tab for providers that have not configured one yet."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)

    def _get_flag(self, customer):
        self.client.force_authenticate(self.staff)
        url = structure_factories.CustomerFactory.get_url(customer)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["has_active_helpdesk"]

    def _provider_with_helpdesk(self, is_active=True):
        customer = structure_factories.CustomerFactory()
        service_provider = marketplace_factories.ServiceProviderFactory(
            customer=customer
        )
        factories.ProviderHelpdeskFactory(
            service_provider=service_provider, is_active=is_active
        )
        return customer

    def test_true_for_provider_with_active_helpdesk(self):
        self.assertTrue(self._get_flag(self._provider_with_helpdesk()))

    def test_false_for_provider_with_inactive_helpdesk(self):
        self.assertFalse(self._get_flag(self._provider_with_helpdesk(is_active=False)))

    def test_false_for_customer_without_helpdesk(self):
        self.assertFalse(self._get_flag(structure_factories.CustomerFactory()))

    @override_config(WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED=False)
    def test_false_when_routing_is_disabled(self):
        self.assertFalse(self._get_flag(self._provider_with_helpdesk()))
