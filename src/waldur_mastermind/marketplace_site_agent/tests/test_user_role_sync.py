import json
from unittest import mock

from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.logging import enums as logging_enums
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING, ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class UserRoleSyncAPITest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.offering = marketplace_factories.OfferingFactory(
            type=SITE_AGENT_OFFERING, project=self.project
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering, project=self.project, state=ResourceStates.OK
        )

        self.staff_user = structure_factories.UserFactory(is_staff=True)

        self.event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.staff_user,
            observable_objects=[
                {"object_type": logging_enums.ObservableObjectType.USER_ROLE.value}
            ],
        )

        # Create subscription queue (required for messages to be sent)
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=self.offering.uuid,
            object_type=logging_enums.ObservableObjectType.USER_ROLE.value,
        )

        self.url = reverse(
            "project-sync-user-roles", kwargs={"uuid": self.project.uuid.hex}
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_user_roles_api_action(self, mocked_publish_messages):
        """Test that the sync_user_roles API action triggers message publishing."""
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url)

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

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_publish_messages.assert_not_called()

    def test_sync_user_roles_permission_denied(self):
        """Test that non-staff users cannot access the sync_user_roles action."""
        regular_user = structure_factories.UserFactory(is_staff=False)
        self.client.force_authenticate(regular_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_user_roles_multiple_offerings(self, mocked_publish_messages):
        """Test that messages are sent for all relevant offerings."""
        offering2 = marketplace_factories.OfferingFactory(
            type=SITE_AGENT_OFFERING, project=self.project
        )
        marketplace_factories.ResourceFactory(
            offering=offering2, project=self.project, state=ResourceStates.OK
        )
        # Create subscription queue for the second offering
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=offering2.uuid,
            object_type=logging_enums.ObservableObjectType.USER_ROLE.value,
        )
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_publish_messages.assert_called_once()

        messages = mocked_publish_messages.call_args[0][0]
        self.assertEqual(len(messages), 2)

        topics = [msg["topic"] for msg in messages]
        self.assertTrue(any(self.offering.uuid.hex in topic for topic in topics))
        self.assertTrue(any(offering2.uuid.hex in topic for topic in topics))

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_user_roles_with_creating_resource(self, mocked_publish_messages):
        """Test that messages are sent when resource is in CREATING state."""
        self.resource.state = ResourceStates.CREATING
        self.resource.save(update_fields=["state"])

        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_publish_messages.assert_called_once()

        messages = mocked_publish_messages.call_args[0][0]
        self.assertEqual(len(messages), 1)

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_sync_user_roles_skips_terminated_resource(self, mocked_publish_messages):
        """Test that no messages are sent when resource is in TERMINATED state."""
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save(update_fields=["state"])

        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_publish_messages.assert_not_called()


class ResourceUserRoleSyncPubSubGateTest(test.APITestCase):
    """The resource-scoped resync trigger publishes a USER_ROLE message
    with resource_uuid, but — like the project trigger — only for
    site-agent offerings."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.staff_user = structure_factories.UserFactory(is_staff=True)

    def _make_resource(self, offering_type):
        offering = marketplace_factories.OfferingFactory(
            type=offering_type,
            project=self.project,
            customer=self.customer,
            plugin_options={"enable_membership_sync_status": True},
        )
        resource = marketplace_factories.ResourceFactory(
            offering=offering, project=self.project, state=ResourceStates.OK
        )
        event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.staff_user,
            observable_objects=[
                {"object_type": logging_enums.ObservableObjectType.USER_ROLE.value}
            ],
        )
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=event_subscription,
            offering_uuid=offering.uuid,
            object_type=logging_enums.ObservableObjectType.USER_ROLE.value,
        )
        return resource

    def _url(self, resource):
        return reverse(
            "marketplace-provider-resource-sync-user-roles",
            kwargs={"uuid": resource.uuid.hex},
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_site_agent_offering_publishes_with_resource_uuid(self, mocked_publish):
        resource = self._make_resource(SITE_AGENT_OFFERING)
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self._url(resource))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_publish.assert_called_once()
        payload = json.loads(mocked_publish.call_args[0][0][0]["payload"])
        self.assertEqual(payload["resource_uuid"], resource.uuid.hex)

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_non_site_agent_offering_publishes_nothing(self, mocked_publish):
        # A non-site-agent offering (e.g. Marketplace.Basic) is gated out —
        # the endpoint still returns 200 but no pubsub message is sent.
        resource = self._make_resource("Marketplace.Basic")
        self.client.force_authenticate(self.staff_user)

        response = self.client.post(self._url(resource))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_publish.assert_not_called()
