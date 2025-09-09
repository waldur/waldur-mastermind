import json
from unittest import mock

from rest_framework import test

from waldur_core.logging import utils as logging_utils
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.structure.enums import ProjectKind
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING
from waldur_mastermind.marketplace.tests import (
    factories as marketplace_factories,
)
from waldur_mastermind.marketplace.tests import (
    fixtures as marketplace_fixtures,
)


class CourseAccountMessageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        project = self.fixture.project
        project.kind = ProjectKind.COURSE
        project.save()
        self.course_account = marketplace_factories.CourseAccountFactory(
            project=self.fixture.project, user=None, email=""
        )
        self.resource = self.fixture.resource
        self.offering = self.fixture.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.save()
        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.fixture.offering_owner,
            observable_objects=[
                {"object_type": logging_utils.ObservableObjectType.COURSE_ACCOUNT.value}
            ],
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_course_account_message_sent(self, mock_publish_messages):
        self.course_account.user = self.fixture.user
        self.course_account.save()

        mock_publish_messages.assert_called_once()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_course_account_deletion_message_sent(self, mock_publish_messages):
        self.course_account.user = self.fixture.user
        self.course_account.save()

        mock_publish_messages.reset_mock()

        self.course_account.set_state_closed()
        self.course_account.save()

        mock_publish_messages.assert_called_once()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_course_account_deletion_message_payload(self, mock_publish_messages):
        """Test that deletion message payload includes action=delete"""
        self.course_account.user = self.fixture.user
        self.course_account.save()

        mock_publish_messages.reset_mock()

        self.course_account.set_state_closed()
        self.course_account.save()

        mock_publish_messages.assert_called_once()
        call_args = mock_publish_messages.call_args[0][0]

        # Verify that the message payload contains action=delete
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
