import json
from unittest import mock

from rest_framework import status, test

from waldur_core.logging import utils as logging_utils
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_site_agent import PLUGIN_NAME


class UserRoleSyncAPITest(test.APITransactionTestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, project=self.project
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering, project=self.project, state=ResourceStates.OK
        )

        self.staff_user = structure_factories.UserFactory(is_staff=True)

        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.staff_user,
            observable_objects=[
                {"object_type": logging_utils.ObservableObjectType.USER_ROLE.value}
            ],
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_user_roles_api_action(self, mocked_publish_messages):
        """Test that the sync_user_roles API action triggers message publishing."""
        self.client.force_authenticate(self.staff_user)
        url = structure_factories.ProjectFactory.get_url(
            self.project, "sync_user_roles"
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_publish_messages.assert_called_once()

        messages = mocked_publish_messages.call_args[0][0]
        self.assertEqual(len(messages), 1)

        message = messages[0]
        self.assertIn("vhost", message)
        self.assertIn("topic", message)
        self.assertIn("payload", message)

        expected_topic_prefix = f"subscription/{self.event_subscription.uuid.hex}/offering/{self.offering.uuid.hex}/user_role"
        self.assertEqual(message["topic"], expected_topic_prefix)

        payload = json.loads(message["payload"])
        self.assertEqual(payload["project_uuid"], self.project.uuid.hex)
        self.assertEqual(payload["project_name"], self.project.name)
        self.assertIn("offering_uuid", payload)

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_user_roles_no_relevant_offerings(self, mocked_publish_messages):
        """Test that no messages are sent when project has no relevant offerings."""
        self.resource.delete()

        self.client.force_authenticate(self.staff_user)
        url = structure_factories.ProjectFactory.get_url(
            self.project, "sync_user_roles"
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_publish_messages.assert_not_called()

    def test_sync_user_roles_permission_denied(self):
        """Test that non-staff users cannot access the sync_user_roles action."""
        regular_user = structure_factories.UserFactory(is_staff=False)
        self.client.force_authenticate(regular_user)
        url = structure_factories.ProjectFactory.get_url(
            self.project, "sync_user_roles"
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_user_roles_multiple_offerings(self, mocked_publish_messages):
        """Test that messages are sent for all relevant offerings."""
        offering2 = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, project=self.project
        )
        marketplace_factories.ResourceFactory(
            offering=offering2, project=self.project, state=ResourceStates.OK
        )
        self.client.force_authenticate(self.staff_user)
        url = structure_factories.ProjectFactory.get_url(
            self.project, "sync_user_roles"
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_publish_messages.assert_called_once()

        messages = mocked_publish_messages.call_args[0][0]
        self.assertEqual(len(messages), 2)

        topics = [msg["topic"] for msg in messages]
        self.assertTrue(any(self.offering.uuid.hex in topic for topic in topics))
        self.assertTrue(any(offering2.uuid.hex in topic for topic in topics))
