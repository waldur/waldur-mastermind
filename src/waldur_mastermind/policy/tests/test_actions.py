from unittest import mock

from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.billing import models as billing_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE
from waldur_mastermind.policy import tasks
from waldur_mastermind.policy.tests import factories


class ActionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.policy = factories.ProjectEstimatedCostPolicyFactory(project=self.project)
        self.estimate = billing_models.PriceEstimate.objects.get(scope=self.project)

        structure_factories.NotificationFactory(
            key='marketplace_policy.notification_about_project_cost_exceeded_limit'
        )

    @mock.patch('waldur_core.core.utils.send_mail')
    def test_notify_project_team(self, mock_send_mail):
        self.fixture.admin

        serialized_scope = core_utils.serialize_instance(self.policy.project)
        serialized_policy = core_utils.serialize_instance(self.policy)
        tasks.notify_about_limit_cost(serialized_scope, serialized_policy)

        mock_send_mail.assert_called_once()

    @mock.patch('waldur_core.core.utils.send_mail')
    def test_notify_organization_owners(self, mock_send_mail):
        self.fixture.owner
        serialized_scope = core_utils.serialize_instance(self.policy.project.customer)
        serialized_policy = core_utils.serialize_instance(self.policy)
        tasks.notify_about_limit_cost(serialized_scope, serialized_policy)

        mock_send_mail.assert_called_once()

    def _create_new_order_item(self):
        order = marketplace_factories.OrderFactory(project=self.project)
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=self.fixture.offering,
            attributes={'name': 'item_name', 'description': 'Description'},
            plan=self.fixture.plan,
        )

        marketplace_utils.process_order_item(order_item, self.fixture.staff)
        order_item.refresh_from_db()
        return order_item

    def test_block_creation_of_new_resources(self):
        self.policy.actions = 'block_creation_of_new_resources'
        self.policy.save()

        order_item = self._create_new_order_item()
        self.assertTrue(order_item.resource)
        self.assertFalse(order_item.error_message)

        self.estimate.total = self.policy.limit_cost + 1
        self.estimate.save()

        order_item = self._create_new_order_item()
        self.assertFalse(order_item.resource)
        self.assertTrue(order_item.error_message)

    def test_block_modification_of_existing_resources(self):
        self.policy.actions = 'block_modification_of_existing_resources'
        self.policy.save()

        order_item = self._create_new_order_item()
        self.assertTrue(order_item.resource)
        self.assertFalse(order_item.error_message)

        self.estimate.total = self.policy.limit_cost + 1
        self.estimate.save()

        order = marketplace_factories.OrderFactory(project=self.project)
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=self.fixture.offering,
            attributes={'name': 'item_name', 'description': 'Description'},
            plan=self.fixture.plan,
            type=marketplace_models.RequestTypeMixin.Types.UPDATE,
            resource=order_item.resource,
        )

        marketplace_utils.process_order_item(order_item, self.fixture.staff)
        order_item.refresh_from_db()

        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.ERRED)
        self.assertTrue(order_item.error_message)

    def test_terminate_resources(self):
        self.policy.actions = 'terminate_resources'
        self.policy.created_by = self.fixture.user
        self.policy.save()

        resource = self.fixture.resource
        resource.state = marketplace_models.Resource.States.OK
        resource.save()

        resource.offering.type = INSTANCE_TYPE
        resource.offering.save()

        self.estimate.total = self.policy.limit_cost + 1
        self.estimate.save()

        self.assertTrue(
            marketplace_models.OrderItem.objects.filter(
                resource=resource,
                type=marketplace_models.OrderItem.Types.TERMINATE,
            ).exists()
        )
        order_item = marketplace_models.OrderItem.objects.filter(
            resource=resource,
            type=marketplace_models.OrderItem.Types.TERMINATE,
        ).get()
        self.assertEqual(order_item.attributes, {'action': 'force_destroy'})
