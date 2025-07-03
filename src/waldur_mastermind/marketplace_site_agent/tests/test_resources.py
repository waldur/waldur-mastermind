from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace_site_agent.tests import (
    fixtures as site_agent_fixtures,
)


class UnlinkTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = site_agent_fixtures.MarketplaceSiteAgentFixture()
        self.url = factories.ResourceFactory.get_url(
            self.fixture.resource, action="unlink"
        )

    def test_unlink(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)

    def test_unlink_erred_resource(self):
        self.client.force_authenticate(self.fixture.staff)
        self.fixture.resource.state = ResourceStates.ERRED
        self.fixture.resource.save()
        self.fixture.resource.scope.state = CoreStates.ERRED
        self.fixture.resource.scope.save()
        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)
