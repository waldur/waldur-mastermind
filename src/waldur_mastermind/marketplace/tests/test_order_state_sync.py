from rest_framework import test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes, ResourceStates
from waldur_mastermind.marketplace.tests import factories


class OrderStateSyncTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.resource = factories.ResourceFactory(
            project=self.project, state=ResourceStates.OK
        )

    def test_resource_state_updating_on_update_order_creation(self):
        factories.OrderFactory(
            project=self.project,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.UPDATE,
            resource=self.resource,
        )
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.UPDATING)

    def test_resource_state_terminating_on_terminate_order_creation(self):
        factories.OrderFactory(
            project=self.project,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.TERMINATE,
            resource=self.resource,
        )
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.TERMINATING)

    def test_resource_state_reverted_on_update_order_cancellation(self):
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()
        order = factories.OrderFactory(
            project=self.project,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.UPDATE,
            resource=self.resource,
        )
        order.state = OrderStates.CANCELED
        order.save()
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)

    def test_resource_state_reverted_on_terminate_order_cancellation(self):
        self.resource.state = ResourceStates.TERMINATING
        self.resource.save()
        order = factories.OrderFactory(
            project=self.project,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.TERMINATE,
            resource=self.resource,
        )
        order.state = OrderStates.CANCELED
        order.save()
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)

    def test_resource_state_reverted_on_update_order_rejection(self):
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()
        order = factories.OrderFactory(
            project=self.project,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.UPDATE,
            resource=self.resource,
        )
        order.state = OrderStates.REJECTED
        order.save()
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)
