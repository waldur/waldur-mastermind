from unittest import mock

from rest_framework import test

from waldur_core.logging import utils as logging_utils
from waldur_core.logging.tests import factories as logging_factories
from waldur_mastermind.marketplace.tests import (
    factories as marketplace_factories,
)
from waldur_mastermind.marketplace.tests import (
    fixtures as marketplace_fixtures,
)
from waldur_mastermind.marketplace_site_agent import PLUGIN_NAME


class ServiceAccountMessageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.service_account = marketplace_factories.ProjectServiceAccountFactory(
            project=self.fixture.project,
            username="",
        )
        self.resource = self.fixture.resource
        self.offering = self.fixture.offering
        self.offering.type = PLUGIN_NAME
        self.offering.save()
        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[
                {
                    "object_type": logging_utils.ObservableObjectType.SERVICE_ACCOUNT.value
                }
            ],
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_service_account_message_sent(self, mock_publish_messages):
        self.service_account.username = "test-username"
        self.service_account.save()

        mock_publish_messages.assert_called_once()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_service_account_deletion_message_sent(self, mock_publish_messages):
        self.service_account.username = "test-username"
        self.service_account.save()

        mock_publish_messages.reset_mock()

        self.service_account.delete()

        mock_publish_messages.assert_called_once()
