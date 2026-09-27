"""Global support reads every reporting endpoint, including the provider-level
statistics and revenue that are otherwise gated on provider roles."""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories, fixtures


class ServiceProviderSupportAccessTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider

    def get(self, user, action):
        self.client.force_authenticate(user)
        return self.client.get(
            factories.ServiceProviderFactory.get_url(self.service_provider, action)
        )

    def test_support_gets_provider_stat_and_revenue(self):
        support = structure_factories.UserFactory(is_support=True)
        for action in ("stat", "revenue"):
            response = self.get(support, action)
            self.assertEqual(response.status_code, status.HTTP_200_OK, action)

    def test_user_without_role_is_denied_provider_stat_and_revenue(self):
        user = structure_factories.UserFactory()
        for action in ("stat", "revenue"):
            response = self.get(user, action)
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                action,
            )
