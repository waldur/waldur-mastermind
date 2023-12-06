from unittest import mock

from django.test import override_settings
from rest_framework import test

from waldur_core.core.utils import serialize_instance
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.models import Order, Resource
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    OrderFactory,
    ResourceFactory,
)
from waldur_mastermind.marketplace.utils import order_should_not_be_reviewed_by_provider
from waldur_mastermind.marketplace_remote import PLUGIN_NAME
from waldur_mastermind.marketplace_remote.tasks import OrderPullTask


class OrderReviewByProviderTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = PLUGIN_NAME
        self.offering.save()
        self.order = self.fixture.order
        self.order.state = marketplace_models.Order.States.PENDING
        self.order.save()

        self.fixture.offering_owner

    def test_option_is_enabled(self):
        self.offering.plugin_options = {'auto_approve_remote_orders': True}
        self.offering.save()

        self.assertTrue(order_should_not_be_reviewed_by_provider(self.order))

    def test_option_is_disabled(self):
        self.offering.plugin_options = {'auto_approve_remote_orders': False}
        self.offering.save()

        self.assertFalse(order_should_not_be_reviewed_by_provider(self.order))

    def test_option_is_absent(self):
        self.assertFalse(order_should_not_be_reviewed_by_provider(self.order))


class LimitsUpdateTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = PLUGIN_NAME
        self.offering.save()

        self.resource = self.fixture.resource
        self.resource.set_state_ok()
        self.resource.save()

        self.plan_component = self.fixture.plan_component
        self.offering_component = self.fixture.offering_component
        self.offering_component.billing_type = (
            marketplace_models.OfferingComponent.BillingTypes.LIMIT
        )
        self.offering_component.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        CustomerRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)

        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        CustomerRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)

    def update_limits(self, user, resource):
        limits = {'cpu': 10}
        customer = self.fixture.customer
        customer.add_user(user, CustomerRole.OWNER)

        self.client.force_authenticate(user)
        url = marketplace_factories.ResourceFactory.get_url(resource, 'update_limits')
        payload = {'limits': limits}
        return self.client.post(url, payload)

    @override_settings(task_always_eager=True)
    @mock.patch('waldur_mastermind.marketplace.utils.process_order')
    def test_order_is_approved_implicitly_for_SP_owner(self, process_order):
        # Act
        user = self.fixture.offering_owner
        response = self.update_limits(user, self.resource)

        # Assert
        self.assertEqual(response.status_code, 200, response.data)
        order = marketplace_models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, marketplace_models.Order.States.EXECUTING)
        self.assertEqual(order.created_by, user)
        process_order.assert_called_once()

    @override_settings(task_always_eager=True)
    @mock.patch('waldur_mastermind.marketplace.utils.process_order')
    def test_order_is_approved_implicitly_for_SP_service_manager(self, process_order):
        # Act
        user = self.fixture.service_manager
        self.offering.add_user(user, OfferingRole.MANAGER)
        response = self.update_limits(user, self.resource)

        # Assert
        self.assertEqual(response.status_code, 200, response.data)
        order = marketplace_models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, marketplace_models.Order.States.EXECUTING)
        self.assertEqual(order.created_by, user)
        process_order.assert_called_once()

    @override_settings(task_always_eager=True)
    @mock.patch('waldur_mastermind.marketplace.utils.process_order')
    def test_order_is_not_approved_for_SP_service_manager_of_another_offering(
        self, process_order
    ):
        # Act
        user = self.fixture.service_manager
        response = self.update_limits(user, self.resource)

        # Assert
        self.assertEqual(response.status_code, 200, response.data)
        order = marketplace_models.Order.objects.get(uuid=response.data['order_uuid'])
        self.assertEqual(order.state, marketplace_models.Order.States.PENDING)
        process_order.assert_not_called()


class OrderPullTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch('waldur_mastermind.marketplace_remote.utils.WaldurClient')
        self.client_mock = patcher.start()
        fixture = ProjectFixture()
        offering = OfferingFactory(
            type=PLUGIN_NAME,
            secret_options={
                'api_url': 'https://remote-waldur.com/',
                'token': 'valid_token',
            },
        )
        self.resource = ResourceFactory(project=fixture.project, offering=offering)
        self.order = OrderFactory(
            project=fixture.project,
            offering=offering,
            resource=self.resource,
            state=Order.States.EXECUTING,
            backend_id='BACKEND_ID',
        )

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_when_order_succeeds_resource_is_updated(self):
        # Arrange
        self.client_mock().get_order.return_value = {
            'state': 'done',
            'error_message': '',
        }

        # Act
        OrderPullTask().run(serialize_instance(self.order))

        # Assert
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, Order.States.DONE)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, Resource.States.OK)

    def test_when_order_fails_resource_is_updated(self):
        # Arrange
        self.client_mock().get_order.return_value = {
            'state': 'erred',
            'error_message': 'Invalid credentials',
        }

        # Act
        OrderPullTask().run(serialize_instance(self.order))

        # Assert
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, Order.States.ERRED)
        self.assertEqual(self.order.error_message, 'Invalid credentials')

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, Resource.States.ERRED)

    def test_when_creation_order_succeeds_resource_is_created(self):
        # Arrange
        self.client_mock().get_order.return_value = {
            'state': 'done',
            'marketplace_resource_uuid': 'marketplace_resource_uuid',
            'error_message': '',
        }
        self.order.resource = None
        self.order.save()

        # Act
        OrderPullTask().run(serialize_instance(self.order))

        # Assert
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.resource)
        self.assertEqual(Resource.States.OK, self.order.resource.state)

    def test_remote_resource_backend_id_is_saved_as_local_resource_effective_id(self):
        # Arrange
        self.client_mock().get_order.return_value = {
            'state': 'done',
            'marketplace_resource_uuid': 'marketplace_resource_uuid',
            'resource_uuid': 'effective_id',
            'error_message': '',
        }
        self.order.resource = None
        self.order.save()

        # Act
        OrderPullTask().run(serialize_instance(self.order))

        # Assert
        self.order.refresh_from_db()
        self.assertEqual('effective_id', self.order.resource.effective_id)
