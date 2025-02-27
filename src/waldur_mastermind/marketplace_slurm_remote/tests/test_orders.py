import datetime
from unittest import mock

from django.utils import timezone
from rest_framework import test

from waldur_core.logging import utils as logging_utils
from waldur_core.logging.tests import factories as logging_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME, tasks


class SendMessagesAboutPendingOrdersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = PLUGIN_NAME
        self.offering.save()

        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            attributes={"name": "item_name", "description": "Description"},
            plan=self.fixture.plan,
            resource=self.fixture.resource,
            state=marketplace_models.Order.States.PENDING_PROVIDER,
        )
        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[
                {"object_type": logging_utils.ObservableObjectType.ORDER.value}
            ],
        )

    @mock.patch("waldur_core.logging.tasks.publish_mqtt_messages.delay")
    def test_send_messages_about_pending_orders(
        self,
        mocked_publish_mqtt_messages,
    ):
        # Arrange
        one_hour_ago = timezone.now() - datetime.timedelta(hours=1)
        self.order.created = one_hour_ago - datetime.timedelta(minutes=1)
        self.order.save()

        # Act
        tasks.send_messages_about_pending_orders()

        # Assert
        mocked_publish_mqtt_messages.assert_called_once()
