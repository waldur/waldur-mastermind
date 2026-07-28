"""State-change events: order/resource transitions are emitted unconditionally.

A single handler per model emits an event for every state transition on any
offering type; consumers (site agents, UI clients) demultiplex on the state in
the payload. Each transition must produce exactly one message per consumer.
"""

from unittest import mock

from rest_framework import test

from waldur_core.logging.tests import factories as logging_factories
from waldur_mastermind.marketplace import enums
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.models import Order
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

RMQ = "aabb000000000000000000000000ccdd"
PUBLISH = "waldur_core.logging.tasks.publish_messages.delay"


class StateChangeEventTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.BASIC_OFFERING
        self.offering.save()
        self.order = self.fixture.order
        self.resource = self.fixture.resource
        self.consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.order.project,
            user=self.fixture.staff,
            queue_created=True,
            rmq_username=RMQ,
        )

    def _consumer_messages(self, mock_publish):
        return [
            m
            for call in mock_publish.call_args_list
            for m in call.args[0]
            if m["topic"] == f"consumer_{self.consumer.uuid.hex}"
        ]


class OrderStateChangeEventTest(StateChangeEventTestBase):
    @mock.patch(PUBLISH)
    def test_order_transition_is_emitted(self, mock_publish):
        self.order.state = OrderStates.EXECUTING
        self.order.save()
        messages = self._consumer_messages(mock_publish)
        self.assertEqual(len(messages), 1)
        self.assertIn('"object_type": "order"', messages[0]["payload"])
        self.assertIn(self.order.uuid.hex, messages[0]["payload"])

    @mock.patch(PUBLISH)
    def test_done_transition_is_emitted(self, mock_publish):
        # The fixture order is created already in DONE state; move it to
        # EXECUTING without firing signals so DONE is a real transition.
        Order.objects.filter(pk=self.order.pk).update(state=OrderStates.EXECUTING)
        order = Order.objects.get(pk=self.order.pk)
        order.state = OrderStates.DONE
        order.save()
        self.assertEqual(len(self._consumer_messages(mock_publish)), 1)

    @mock.patch(PUBLISH)
    def test_pending_transition_is_emitted_exactly_once(self, mock_publish):
        self.order.state = OrderStates.PENDING_PROVIDER
        self.order.save()
        self.assertEqual(len(self._consumer_messages(mock_publish)), 1)

    @mock.patch(PUBLISH)
    def test_site_agent_offering_transition_is_emitted_exactly_once(self, mock_publish):
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()
        self.order.state = OrderStates.PENDING_PROVIDER
        self.order.save()
        self.assertEqual(len(self._consumer_messages(mock_publish)), 1)

    @mock.patch(PUBLISH)
    def test_save_without_state_change_is_silent(self, mock_publish):
        self.order.save()
        self.assertEqual(len(self._consumer_messages(mock_publish)), 0)

    @mock.patch(PUBLISH)
    def test_order_creation_is_silent(self, mock_publish):
        # Creation is not a transition; the initial state is delivered by the
        # first real transition (or fetched by the consumer directly).
        marketplace_factories.OrderFactory(
            project=self.order.project,
            offering=self.offering,
            state=OrderStates.PENDING_CONSUMER,
        )
        # The factory also creates a Resource, which legitimately emits a
        # resource-creation event — only order events must stay silent.
        order_messages = [
            m
            for m in self._consumer_messages(mock_publish)
            if '"object_type": "order"' in m["payload"]
        ]
        self.assertEqual(len(order_messages), 0)


class ResourceStateChangeEventTest(StateChangeEventTestBase):
    @mock.patch(PUBLISH)
    def test_resource_transition_is_emitted(self, mock_publish):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        messages = self._consumer_messages(mock_publish)
        self.assertEqual(len(messages), 1)
        self.assertIn('"object_type": "resource"', messages[0]["payload"])
        self.assertIn('"resource_state": "OK"', messages[0]["payload"])

    @mock.patch(PUBLISH)
    def test_resource_creation_is_emitted(self, mock_publish):
        marketplace_factories.ResourceFactory(
            project=self.order.project,
            offering=self.offering,
        )
        self.assertEqual(len(self._consumer_messages(mock_publish)), 1)

    @mock.patch(PUBLISH)
    def test_save_without_state_change_is_silent(self, mock_publish):
        self.resource.save()
        self.assertEqual(len(self._consumer_messages(mock_publish)), 0)
