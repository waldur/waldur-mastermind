import uuid
from unittest import mock

import respx
from django.test import override_settings
from rest_framework import test
from waldur_api_client.errors import UnexpectedStatus

from waldur_core.core.utils import serialize_instance
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    BillingTypes,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    OrderFactory,
    ResourceFactory,
)
from waldur_mastermind.marketplace.utils import order_should_not_be_reviewed_by_provider
from waldur_mastermind.marketplace_remote.processors import (
    RemoteUpdateResourceProcessor,
)
from waldur_mastermind.marketplace_remote.tasks import OrderPullTask
from waldur_mastermind.marketplace_remote.tests.dns_utils import (
    create_selective_dns_mock,
)


class OrderReviewByProviderTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = REMOTE_OFFERING
        self.offering.save()
        self.order = self.fixture.order
        self.order.state = OrderStates.PENDING_PROVIDER
        self.order.save()

        self.fixture.offering_owner

    def test_option_is_enabled(self):
        self.offering.plugin_options = {"auto_approve_remote_orders": True}
        self.offering.save()

        self.assertTrue(order_should_not_be_reviewed_by_provider(self.order))

    def test_option_is_disabled(self):
        self.offering.plugin_options = {"auto_approve_remote_orders": False}
        self.offering.save()

        self.assertFalse(order_should_not_be_reviewed_by_provider(self.order))

    def test_option_is_absent(self):
        self.assertFalse(order_should_not_be_reviewed_by_provider(self.order))


class LimitsUpdateTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = REMOTE_OFFERING
        self.offering.save()

        self.resource = self.fixture.resource
        self.resource.set_state_ok()
        self.resource.save()

        self.plan_component = self.fixture.plan_component
        self.offering_component = self.fixture.offering_component
        self.offering_component.billing_type = BillingTypes.LIMIT
        self.offering_component.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_RESOURCE_LIMITS
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)

    def update_limits(self, user, resource):
        limits = {"cpu": 10}
        customer = self.fixture.customer
        customer.add_user(user, CustomerRole.OWNER)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)

        self.client.force_authenticate(user)
        url = marketplace_factories.ResourceFactory.get_url(resource, "update_limits")
        payload = {"limits": limits}
        return self.client.post(url, payload)

    @override_settings(task_always_eager=True)
    @mock.patch("waldur_mastermind.marketplace.utils.process_order")
    def test_order_is_approved_implicitly_for_SP_owner(self, process_order):
        # Act
        user = self.fixture.offering_owner
        response = self.update_limits(user, self.resource)

        # Assert
        self.assertEqual(response.status_code, 200, response.data)
        order = marketplace_models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(order.created_by, user)
        process_order.assert_called_once()

    @override_settings(task_always_eager=True)
    @mock.patch("waldur_mastermind.marketplace.utils.process_order")
    def test_order_is_approved_implicitly_for_SP_service_manager(self, process_order):
        # Act
        user = self.fixture.service_manager
        self.offering.add_user(user, OfferingRole.MANAGER)
        response = self.update_limits(user, self.resource)

        # Assert
        self.assertEqual(response.status_code, 200, response.data)
        order = marketplace_models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(order.created_by, user)
        process_order.assert_called_once()

    @override_settings(task_always_eager=True)
    @mock.patch("waldur_mastermind.marketplace.utils.process_order")
    def test_order_is_not_approved_for_SP_service_manager_of_another_offering(
        self, process_order
    ):
        # Act
        user = self.fixture.service_manager
        response = self.update_limits(user, self.resource)

        # Assert
        self.assertEqual(response.status_code, 200, response.data)
        order = marketplace_models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)
        process_order.assert_not_called()


class OrderPullTest(test.APITestCase):
    def setUp(self) -> None:
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        super().setUp()
        respx.start()
        fixture = ProjectFixture()
        self.api_url = "https://remote-waldur.com"
        offering = OfferingFactory(
            type=REMOTE_OFFERING,
            secret_options={
                "api_url": self.api_url,
                "token": "valid_token",
            },
        )
        self.resource = ResourceFactory(project=fixture.project, offering=offering)
        self.backend_id = uuid.uuid4().hex
        self.order = OrderFactory(
            project=fixture.project,
            offering=offering,
            resource=self.resource,
            state=OrderStates.EXECUTING,
            backend_id=self.backend_id,
        )

    def tearDown(self):
        self.dns_patcher.stop()
        super().tearDown()
        respx.stop()
        mock.patch.stopall()

    def mock_order_response(
        self, state, error_message="", marketplace_resource_uuid=None
    ):
        response_json = {"state": state, "error_message": error_message}
        if marketplace_resource_uuid:
            response_json["marketplace_resource_uuid"] = marketplace_resource_uuid
        respx.get(f"{self.api_url}/api/marketplace-orders/{self.backend_id}/").respond(
            200, json=response_json
        )

    def test_when_order_succeeds_resource_is_updated(self):
        # Arrange
        self.mock_order_response(state="done")

        # Act
        OrderPullTask().run(serialize_instance(self.order))

        # Assert
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.DONE)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)

    def test_when_order_fails_resource_is_updated(self):
        # Arrange
        self.mock_order_response(state="erred", error_message="Invalid credentials")

        # Act
        OrderPullTask().run(serialize_instance(self.order))

        # Assert
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.ERRED)
        self.assertEqual(self.order.error_message, "Invalid credentials")

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.ERRED)

    def test_when_creation_order_succeeds_resource_is_created(self):
        # Arrange
        self.mock_order_response(
            state="done", marketplace_resource_uuid=uuid.uuid4().hex
        )

        # Act
        OrderPullTask().run(serialize_instance(self.order))

        # Assert
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.resource)
        self.assertEqual(ResourceStates.OK, self.order.resource.state)


class RemoteUpdateResourceProcessorTest(test.APITestCase):
    def setUp(self):
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        super().setUp()
        respx.start()

        fixture = ProjectFixture()
        self.api_url = "https://remote-waldur.com"
        offering = OfferingFactory(
            type=REMOTE_OFFERING,
            secret_options={
                "api_url": self.api_url,
                "token": "valid_token",
            },
        )
        self.resource = ResourceFactory(project=fixture.project, offering=offering)
        self.resource.backend_id = uuid.uuid4().hex
        self.resource.save()

        self.user = fixture.owner

        self.order = OrderFactory(
            project=fixture.project,
            offering=offering,
            resource=self.resource,
            state=OrderStates.EXECUTING,
            type=OrderTypes.UPDATE,
            limits={"cpu": 10, "ram": 20},
            attributes={"old_limits": {"cpu": 5, "ram": 10}},
        )

    def tearDown(self):
        self.dns_patcher.stop()
        super().tearDown()
        respx.stop()
        mock.patch.stopall()

    def _mock_resource_endpoints(self, get_response, post_response):
        """Helper method to mock both GET and POST endpoints for a resource."""
        resource_uuid = str(uuid.UUID(self.resource.backend_id))
        self.get_mock = respx.get(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/"
        ).respond(**get_response)
        self.post_mock = respx.post(
            f"{self.api_url}/api/marketplace-resources/{resource_uuid}/update_limits/"
        ).respond(**post_response)

    def test_update_limits_when_remote_already_has_same_limits(self):
        """Test that order completes successfully when remote limits already match."""
        self._mock_resource_endpoints(
            get_response={
                "status_code": 200,
                "json": {
                    "uuid": str(uuid.UUID(self.resource.backend_id)),
                    "limits": {"cpu": 10, "ram": 20},
                },
            },
            post_response={
                "status_code": 200,
                "json": {"order_uuid": uuid.uuid4().hex},
            },
        )

        processor = RemoteUpdateResourceProcessor(self.order)
        processor.process_order(user=self.user)

        self.assertEqual(self.order.state, OrderStates.DONE)
        self.assertIn(
            "Remote limits already match requested limits. No update needed.",
            self.order.output,
        )
        self.assertIsNotNone(self.order.output_updated_at)
        self.assertEqual(self.get_mock.call_count, 1)
        self.assertEqual(self.post_mock.call_count, 0)

    def test_update_limits_when_remote_has_different_limits(self):
        """Test that order proceeds with update when remote limits are different."""
        self._mock_resource_endpoints(
            get_response={
                "status_code": 200,
                "json": {
                    "uuid": str(uuid.UUID(self.resource.backend_id)),
                    "limits": {"cpu": 5, "ram": 10},
                },
            },
            post_response={
                "status_code": 200,
                "json": {"order_uuid": uuid.uuid4().hex},
            },
        )

        processor = RemoteUpdateResourceProcessor(self.order)
        result = processor.update_limits_process(user=self.user)

        self.assertFalse(result)
        self.assertIsNotNone(self.order.backend_id)
        self.assertEqual(self.get_mock.call_count, 1)
        self.assertEqual(self.post_mock.call_count, 1)

    def test_update_limits_when_remote_api_returns_400_same_limits(self):
        """Test that order completes successfully when remote API returns 400 for same limits."""
        self._mock_resource_endpoints(
            get_response={
                "status_code": 500,
                "json": {"error": "Internal server error"},
            },
            post_response={
                "status_code": 400,
                "json": [
                    "Impossible to create update orders with limits set to exactly the same."
                ],
            },
        )

        processor = RemoteUpdateResourceProcessor(self.order)
        processor.process_order(user=self.user)

        self.assertEqual(self.order.state, OrderStates.DONE)
        self.assertIn(
            "Remote limits already match requested limits. No update needed.",
            self.order.output,
        )
        self.assertIsNotNone(self.order.output_updated_at)
        self.assertEqual(self.get_mock.call_count, 1)
        self.assertEqual(self.post_mock.call_count, 1)

    def test_update_limits_when_remote_api_returns_400_different_error(self):
        """Test that order fails when remote API returns 400 for different reason."""
        self._mock_resource_endpoints(
            get_response={
                "status_code": 500,
                "json": {"error": "Internal server error"},
            },
            post_response={"status_code": 400, "json": ["Invalid limits provided"]},
        )

        processor = RemoteUpdateResourceProcessor(self.order)

        with self.assertRaises(UnexpectedStatus):
            processor.update_limits_process(user=self.user)
