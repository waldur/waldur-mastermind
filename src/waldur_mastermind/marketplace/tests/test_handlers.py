from django.test import TransactionTestCase
from mock import patch

from waldur_core.structure.tests import fixtures

from . import factories


class OrderHandlersTest(TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_order_approval_notifications_sent_out_if_just_created(self):
        with patch('waldur_mastermind.marketplace.tasks.notify_order_approvers') as mocked_task:
            factories.OrderFactory(project=self.fixture.project, created_by=self.fixture.staff)
            mocked_task.delay.assert_called()

    def test_order_approval_notifications_not_sent_out_if_executing_state(self):
        with patch('waldur_mastermind.marketplace.tasks.notify_order_approvers') as mocked_task:
            order = factories.OrderFactory.build(project=self.fixture.project, created_by=self.fixture.staff)
            order.approve()
            order.save()
            mocked_task.delay.assert_not_called()
