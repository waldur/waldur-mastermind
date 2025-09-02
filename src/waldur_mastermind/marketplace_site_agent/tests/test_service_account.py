from unittest import mock

from rest_framework import test

from waldur_core.logging import utils as logging_utils
from waldur_core.logging.tests import factories as logging_factories
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING, ServiceAccountState
from waldur_mastermind.marketplace.tests import (
    factories as marketplace_factories,
)
from waldur_mastermind.marketplace.tests import (
    fixtures as marketplace_fixtures,
)


class ServiceAccountMessageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.service_account = marketplace_factories.ProjectServiceAccountFactory(
            project=self.fixture.project,
            username="",
        )
        self.resource = self.fixture.resource
        self.offering = self.fixture.offering
        self.offering.type = SITE_AGENT_OFFERING
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

        self.service_account.set_state_closed()
        self.service_account.save()

        mock_publish_messages.assert_called_once()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_service_account_deletion_message_payload(self, mock_publish_messages):
        """Test that deletion message payload includes action=delete"""
        self.service_account.username = "test-username"
        self.service_account.save()

        mock_publish_messages.reset_mock()

        self.service_account.set_state_closed()
        self.service_account.save()

        mock_publish_messages.assert_called_once()
        call_args = mock_publish_messages.call_args[0][0]

        # Verify that the message payload contains action=delete
        import json

        has_delete_action = False
        for message in call_args:
            if isinstance(message, dict):
                payload_str = message.get("payload", "{}")
                if isinstance(payload_str, str):
                    try:
                        payload = json.loads(payload_str)
                        if payload.get("action") == "delete":
                            has_delete_action = True
                            break
                    except json.JSONDecodeError:
                        continue

        self.assertTrue(has_delete_action, "No delete action found in messages")

    def test_service_account_default_state_is_ok(self):
        """Test that newly created service accounts have OK state by default"""
        self.assertEqual(self.service_account.state, ServiceAccountState.OK)

    def test_service_account_can_transition_to_closed(self):
        """Test that service accounts can transition from OK to CLOSED"""
        self.service_account.set_state_closed()
        self.service_account.save()
        self.assertEqual(self.service_account.state, ServiceAccountState.CLOSED)

    def test_service_account_can_transition_to_erred(self):
        """Test that service accounts can transition to ERRED from any state"""
        self.service_account.set_state_erred()
        self.service_account.save()
        self.assertEqual(self.service_account.state, ServiceAccountState.ERRED)

        # Can transition from ERRED to CLOSED
        self.service_account.set_state_closed()
        self.service_account.save()
        self.assertEqual(self.service_account.state, ServiceAccountState.CLOSED)

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_no_deletion_message_sent_on_state_change_to_erred(
        self, mock_publish_messages
    ):
        """Test that deletion message is NOT sent when transitioning to ERRED state"""
        self.service_account.username = "test-username"
        self.service_account.save()

        mock_publish_messages.reset_mock()

        self.service_account.set_state_erred()
        self.service_account.save()

        mock_publish_messages.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_no_deletion_message_sent_on_model_deletion(self, mock_publish_messages):
        """Test that deletion message is NOT sent when model is actually deleted"""
        self.service_account.username = "test-username"
        self.service_account.save()

        mock_publish_messages.reset_mock()

        # Hard delete the model (not soft delete via state change)
        self.service_account.delete()

        mock_publish_messages.assert_not_called()
