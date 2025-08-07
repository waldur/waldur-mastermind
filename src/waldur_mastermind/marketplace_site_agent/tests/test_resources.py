from unittest import mock

from django.test import override_settings
from rest_framework import status, test

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
        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)


class ResourcePullTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = site_agent_fixtures.MarketplaceSiteAgentFixture()
        resource = self.fixture.resource
        resource.state = ResourceStates.OK
        resource.backend_id = "test-backend-id"
        resource.scope = None
        resource.save()
        self.url = factories.ResourceFactory.get_url(
            self.fixture.resource, action="pull"
        )

    @override_settings(task_always_eager=True)
    @mock.patch(
        "waldur_mastermind.marketplace_site_agent.utils.push_resource_update_message"
    )
    def test_resource_pull(self, mock_push_resource_update_message):
        mock_push_resource_update_message.return_value = None
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_202_ACCEPTED, response.status_code, response.data)
        mock_push_resource_update_message.assert_called_once()
