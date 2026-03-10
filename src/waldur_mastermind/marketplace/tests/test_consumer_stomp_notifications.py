"""Tests that STOMP notifications reach offering consumers (not just providers)."""

import json

from django.test import TestCase

from waldur_core.logging import enums as logging_enums
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace.utils import prepare_messages


def _setup_subscription(user, offering, object_type):
    """Create event subscription and queue for the given user, offering and event type."""
    event_subscription = logging_factories.EventSubscriptionFactory(
        user=user,
        observable_objects=[{"object_type": object_type.value}],
    )
    logging_factories.EventSubscriptionQueueFactory(
        event_subscription=event_subscription,
        offering_uuid=offering.uuid,
        object_type=object_type.value,
    )
    return event_subscription


class TestConsumerReceivesStompNotifications(TestCase):
    """Consumer-side users should receive STOMP messages for offerings they have resources on."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        # Ensure the resource is in OK state (not terminated)
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()

        # The consumer user (owner of the customer org that has a resource on the offering)
        self.consumer_user = self.fixture.owner

    def test_consumer_user_receives_order_message(self):
        """A user whose organization has active resources on the offering gets ORDER messages."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.ORDER,
        )

        messages = prepare_messages(
            self.fixture.offering,
            {
                "order_uuid": self.fixture.order.uuid.hex,
                "order_state": "done",
            },
            logging_enums.ObservableObjectType.ORDER,
        )

        self.assertEqual(len(messages), 1)
        payload = json.loads(messages[0]["payload"])
        self.assertEqual(payload["order_state"], "done")
        self.assertEqual(messages[0]["vhost"], self.consumer_user.uuid.hex)
        payload = json.loads(messages[0]["payload"])
        self.assertEqual(payload["order_state"], "done")
        self.assertEqual(messages[0]["vhost"], self.consumer_user.uuid.hex)

    def test_consumer_does_not_receive_order_for_other_customer(self):
        """Consumer must NOT get ORDER events for orders belonging to another customer."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.ORDER,
        )

        # Create an order from a different customer's project on the same offering
        other_project = structure_factories.ProjectFactory()
        other_order = marketplace_factories.OrderFactory(
            offering=self.fixture.offering,
            project=other_project,
        )

        messages = prepare_messages(
            self.fixture.offering,
            {
                "order_uuid": other_order.uuid.hex,
                "order_state": "done",
            },
            logging_enums.ObservableObjectType.ORDER,
        )

        self.assertEqual(messages, [])

    def test_consumer_user_with_terminated_resource_excluded(self):
        """A user whose organization only has terminated resources is excluded."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.ORDER,
        )

        self.fixture.resource.state = ResourceStates.TERMINATED
        self.fixture.resource.save()

        messages = prepare_messages(
            self.fixture.offering,
            {
                "order_uuid": self.fixture.order.uuid.hex,
                "order_state": "done",
            },
            logging_enums.ObservableObjectType.ORDER,
        )

        self.assertEqual(messages, [])

    def test_provider_user_still_receives_messages(self):
        """Provider-side users continue to receive messages as before."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.ORDER,
        )
        provider_user = self.fixture.service_owner
        _setup_subscription(
            provider_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.ORDER,
        )

        messages = prepare_messages(
            self.fixture.offering,
            {
                "order_uuid": self.fixture.order.uuid.hex,
                "order_state": "done",
            },
            logging_enums.ObservableObjectType.ORDER,
        )

        # Both consumer and provider should receive messages
        vhosts = {m["vhost"] for m in messages}
        self.assertIn(provider_user.uuid.hex, vhosts)
        self.assertIn(self.consumer_user.uuid.hex, vhosts)

    def test_unrelated_user_excluded(self):
        """A user with no relationship to the offering is excluded."""
        unrelated_user = structure_factories.UserFactory()
        _setup_subscription(
            unrelated_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.ORDER,
        )

        messages = prepare_messages(
            self.fixture.offering,
            {
                "order_uuid": self.fixture.order.uuid.hex,
                "order_state": "done",
            },
            logging_enums.ObservableObjectType.ORDER,
        )

        self.assertEqual(messages, [])

    def test_consumer_receives_resource_event_for_own_resource(self):
        """Consumer gets RESOURCE events for resources in their projects."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.RESOURCE,
        )

        messages = prepare_messages(
            self.fixture.offering,
            {
                "resource_uuid": self.fixture.resource.uuid.hex,
                "resource_backend_id": "test-backend-id",
            },
            logging_enums.ObservableObjectType.RESOURCE,
        )

        self.assertEqual(len(messages), 1)

    def test_consumer_does_not_receive_resource_event_for_other_customer(self):
        """Consumer must NOT get RESOURCE events for another customer's resources."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.RESOURCE,
        )

        other_project = structure_factories.ProjectFactory()
        other_resource = marketplace_factories.ResourceFactory(
            offering=self.fixture.offering,
            project=other_project,
        )

        messages = prepare_messages(
            self.fixture.offering,
            {
                "resource_uuid": other_resource.uuid.hex,
                "resource_backend_id": "other-backend-id",
            },
            logging_enums.ObservableObjectType.RESOURCE,
        )

        self.assertEqual(messages, [])

    def test_consumer_receives_offering_user_event_for_own_project_user(self):
        """Consumer gets OFFERING_USER events for users in their projects."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.OFFERING_USER,
        )

        # The fixture.manager is a member of the consumer's project
        project_member = self.fixture.manager

        messages = prepare_messages(
            self.fixture.offering,
            {
                "offering_user_uuid": "fake-uuid",
                "user_uuid": project_member.uuid.hex,
                "action": "create",
            },
            logging_enums.ObservableObjectType.OFFERING_USER,
        )

        self.assertEqual(len(messages), 1)

    def test_consumer_does_not_receive_offering_user_event_for_unrelated_user(self):
        """Consumer must NOT get OFFERING_USER events for users outside their projects."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.OFFERING_USER,
        )

        # A user that is NOT in any of the consumer's projects
        unrelated_user = structure_factories.UserFactory()

        messages = prepare_messages(
            self.fixture.offering,
            {
                "offering_user_uuid": "fake-uuid",
                "user_uuid": unrelated_user.uuid.hex,
                "action": "create",
            },
            logging_enums.ObservableObjectType.OFFERING_USER,
        )

        self.assertEqual(messages, [])

    def test_consumer_receives_user_role_event_for_own_project(self):
        """Consumer gets USER_ROLE events for their own projects."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.USER_ROLE,
        )

        messages = prepare_messages(
            self.fixture.offering,
            {
                "user_uuid": self.fixture.manager.uuid.hex,
                "project_uuid": self.fixture.project.uuid.hex,
                "project_name": self.fixture.project.name,
                "role_name": "admin",
                "granted": True,
            },
            logging_enums.ObservableObjectType.USER_ROLE,
        )

        self.assertEqual(len(messages), 1)

    def test_consumer_does_not_receive_user_role_event_for_other_project(self):
        """Consumer must NOT get USER_ROLE events for projects outside their org."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.USER_ROLE,
        )

        other_project = structure_factories.ProjectFactory()

        messages = prepare_messages(
            self.fixture.offering,
            {
                "user_uuid": "fake-user-uuid",
                "project_uuid": other_project.uuid.hex,
                "project_name": other_project.name,
                "role_name": "admin",
                "granted": True,
            },
            logging_enums.ObservableObjectType.USER_ROLE,
        )

        self.assertEqual(messages, [])

    def test_consumer_receives_service_account_event_for_own_project(self):
        """Consumer gets SERVICE_ACCOUNT events for their own projects."""
        _setup_subscription(
            self.consumer_user,
            self.fixture.offering,
            logging_enums.ObservableObjectType.SERVICE_ACCOUNT,
        )

        messages = prepare_messages(
            self.fixture.offering,
            {
                "account_uuid": "fake-uuid",
                "account_username": "svc-test",
                "scope_type": "project",
                "project_uuid": self.fixture.project.uuid.hex,
                "project_name": self.fixture.project.name,
                "action": "create",
            },
            logging_enums.ObservableObjectType.SERVICE_ACCOUNT,
        )

        self.assertEqual(len(messages), 1)
