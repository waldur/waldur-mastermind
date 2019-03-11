from django.test import TransactionTestCase
from mock import patch

from waldur_core.structure.tests import fixtures, factories as structure_factories
from waldur_mastermind.marketplace import handlers as marketplace_handlers
from waldur_core.structure.tests import models as structure_tests_models

from . import factories


class OrderHandlersTest(TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_order_approval_notifications_sent_out_if_just_created(self):
        with patch('waldur_mastermind.marketplace.tasks.notify_order_approvers') as mocked_task:
            factories.OrderFactory(project=self.fixture.project, created_by=self.fixture.manager)
            mocked_task.delay.assert_called()

    def test_order_approval_notifications_does_not_sent_out_if_approval_is_auto(self):
        with patch('waldur_mastermind.marketplace.tasks.notify_order_approvers') as mocked_task:
            factories.OrderFactory(project=self.fixture.project, created_by=self.fixture.staff)
            mocked_task.delay.assert_not_called()

    def test_order_approval_notifications_not_sent_out_if_executing_state(self):
        with patch('waldur_mastermind.marketplace.tasks.notify_order_approvers') as mocked_task:
            order = factories.OrderFactory.build(project=self.fixture.project, created_by=self.fixture.staff)
            order.approve()
            order.save()
            mocked_task.delay.assert_not_called()

    def test_marketplace_resource_name_should_be_updated_if_resource_name_in_plugin_is_updated(self):
        marketplace_handlers.connect_resource_metadata_handlers(structure_tests_models.TestNewInstance)
        instance = structure_factories.TestNewInstanceFactory()
        resource = factories.ResourceFactory(scope=instance)
        instance.name = 'New name'
        instance.save()
        resource.refresh_from_db()
        self.assertEqual(resource.attributes['name'], 'New name')
