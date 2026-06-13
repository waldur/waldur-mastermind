import json
from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.logging import enums as logging_enums
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING, OfferingStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


@ddt
class OfferingResourcesSyncTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION
        )
        self.offering = self.fixture.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.staff,
            observable_objects=[
                {
                    "object_type": logging_enums.ObservableObjectType.OFFERING_RESOURCES_SYNC.value
                }
            ],
        )
        # Create subscription queue (required for messages to be sent)
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=self.offering.uuid,
            object_type=logging_enums.ObservableObjectType.OFFERING_RESOURCES_SYNC.value,
        )

        self.url = marketplace_factories.OfferingFactory.get_url(
            self.offering, "sync_resources"
        )

    @data("staff", "service_owner", "service_manager")
    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_resources_publishes_message(self, user, mocked_publish_messages):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mocked_publish_messages.assert_called_once()

        messages = mocked_publish_messages.call_args[0][0]
        self.assertEqual(len(messages), 1)

        message = messages[0]
        expected_topic = (
            f"subscription/{self.event_subscription.uuid.hex}"
            f"/offering/{self.offering.uuid.hex}/offering_resources_sync"
        )
        self.assertEqual(message["topic"], expected_topic)

        payload = json.loads(message["payload"])
        self.assertEqual(payload["offering_uuid"], self.offering.uuid.hex)
        self.assertEqual(payload["requested_by_user_uuid"], user.uuid.hex)

    @data("admin", "owner", "manager")
    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_resources_forbidden(self, user, mocked_publish_messages):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mocked_publish_messages.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_resources_rejects_non_site_agent_offering(
        self, mocked_publish_messages
    ):
        offering = marketplace_factories.OfferingFactory(
            customer=self.offering.customer,
            state=OfferingStates.ACTIVE,
        )
        url = marketplace_factories.OfferingFactory.get_url(offering, "sync_resources")
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mocked_publish_messages.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_resources_without_subscribed_agent(self, mocked_publish_messages):
        offering = marketplace_factories.OfferingFactory(
            customer=self.offering.customer,
            type=SITE_AGENT_OFFERING,
            state=OfferingStates.ACTIVE,
        )
        url = marketplace_factories.OfferingFactory.get_url(offering, "sync_resources")
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        mocked_publish_messages.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_resources_validates_offering_state(self, mocked_publish_messages):
        self.offering.state = OfferingStates.DRAFT
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url)

        # StateValidator raises IncorrectStateException which maps to 409
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        mocked_publish_messages.assert_not_called()
