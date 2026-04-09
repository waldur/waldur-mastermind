import datetime
from unittest import mock

from constance.test.unittest import override_config
from ddt import data, ddt
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    SCRIPT_OFFERING,
    BillingTypes,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tasks import process_order
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class OrderApproveByConsumerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)

    def test_owner_can_approve_order(self):
        self.ensure_user_can_approve_order(self.fixture.owner)

    def test_by_default_manager_can_not_approve_order(self):
        self.ensure_user_can_not_approve_order(self.fixture.manager)

    def test_by_default_admin_can_not_approve_order(self):
        self.ensure_user_can_not_approve_order(self.fixture.admin)

    def test_manager_can_approve_order_if_feature_is_enabled(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.ensure_user_can_approve_order(self.fixture.manager)

    def test_admin_can_approve_order_if_feature_is_enabled(self):
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.ensure_user_can_approve_order(self.fixture.admin)

    def test_user_can_not_reapprove_active_order(self):
        self.order.state = OrderStates.EXECUTING
        self.order.save()
        response = self.approve_order(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.order.consumer_reviewed_by, None)

    def test_order_approving_is_not_available_for_blocked_organization(self):
        self.order.project.customer.blocked = True
        self.order.project.customer.save()
        response = self.approve_order(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay")
    def test_order_with_basic_offering_is_approved_by_consumer_it_is_pending_for_provider_review_too(
        self, mocked_delay
    ):
        mocked_delay.side_effect = process_order
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=BASIC_OFFERING
        )
        order = factories.OrderFactory(
            offering=offering, project=self.project, created_by=self.manager
        )
        self.approve_order(self.fixture.owner, order)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)

    def test_user_cannot_approve_order_if_project_is_expired(self):
        self.project.end_date = datetime.datetime(year=2020, month=1, day=1).date()
        self.project.save()

        with freeze_time("2020-01-01"):
            response = self.approve_order(self.fixture.staff)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_order_approval_handles_project_start_date_correctly(self):
        """Test that project start_date (DateField) is correctly compared with timezone.now()."""
        # Set a future start date (as date, not datetime)
        future_date = datetime.datetime(year=2030, month=1, day=1).date()
        self.project.start_date = future_date
        self.project.save()

        # Order should go to PENDING_PROJECT state when project has future start date
        response = self.approve_order(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.PENDING_PROJECT)

        # Set a past start date
        past_date = datetime.datetime(year=2020, month=1, day=1).date()
        self.project.start_date = past_date
        self.project.save()

        # Reset order state
        self.order.state = OrderStates.PENDING_CONSUMER
        self.order.save()

        # Order should not go to PENDING_PROJECT state when project has past start date
        response = self.approve_order(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.state, OrderStates.PENDING_PROJECT)

    def test_project_start_date_has_priority_over_order_start_date(self):
        """
        Ensure if both project and order have future start dates,
        the order waits for the project first.
        """
        # Arrange
        future_project_date = (timezone.now() + datetime.timedelta(days=20)).date()
        self.project.start_date = future_project_date
        self.project.save()

        future_order_date = (timezone.now() + datetime.timedelta(days=10)).date()
        self.order.start_date = future_order_date
        self.order.save()

        # Act
        response = self.approve_order(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.PENDING_PROJECT)

    @mock.patch(
        "waldur_mastermind.marketplace.utils.order_should_not_be_reviewed_by_provider",
        return_value=True,
    )
    @override_config(ENABLE_ORDER_START_DATE=True)
    def test_order_goes_to_pending_start_date_if_no_provider_review_is_needed(
        self, mock_should_skip
    ):
        """
        If provider review is skipped, the order should move to PENDING_START_DATE
        if its own start_date is in the future.
        """
        # Arrange
        future_order_date = (timezone.now() + datetime.timedelta(days=10)).date()
        self.order.start_date = future_order_date
        self.order.save()

        # Act
        response = self.approve_order(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.PENDING_START_DATE)
        mock_should_skip.assert_called_once_with(self.order)

    def test_order_goes_to_pending_provider_even_if_start_date_is_set(self):
        """
        If provider review IS required, the order must go to PENDING_PROVIDER,
        ignoring its own future start_date for now.
        """
        # Arrange
        # Use an offering that requires provider approval (like BASIC_OFFERING)
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=BASIC_OFFERING
        )
        order = factories.OrderFactory(
            offering=offering,
            project=self.project,
            created_by=self.manager,
            state=OrderStates.PENDING_CONSUMER,
        )
        future_order_date = (timezone.now() + datetime.timedelta(days=10)).date()
        order.start_date = future_order_date
        order.save()

        # Act
        response = self.approve_order(self.fixture.owner, order)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)

    def approve_order(self, user, order=None):
        order = order or self.order
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(order, "approve_by_consumer")
        response = self.client.post(url)
        order.refresh_from_db()
        return response

    def ensure_user_can_approve_order(self, user):
        response = self.approve_order(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.order.consumer_reviewed_by, user)

    def ensure_user_can_not_approve_order(self, user):
        response = self.approve_order(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.order.consumer_reviewed_by, None)


@override_settings(task_always_eager=True)
class OrderApproveByProviderTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)

    @override_config(ENABLE_ORDER_START_DATE=True)
    def test_order_goes_to_pending_start_date_after_provider_approval(self):
        """
        If an order has a future start date, after provider approval,
        it should transition to PENDING_START_DATE.
        """
        # Arrange
        future_order_date = (timezone.now() + datetime.timedelta(days=10)).date()
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=BASIC_OFFERING,
        )
        order = factories.OrderFactory(
            offering=offering,
            project=self.project,
            created_by=self.manager,
            state=OrderStates.PENDING_PROVIDER,
            start_date=future_order_date,
        )

        # Act
        response = self.approve_order(self.fixture.owner, order)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.PENDING_START_DATE)

    def test_when_update_order_with_basic_offering_is_approved_resource_is_marked_as_ok(
        self,
    ):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=BASIC_OFFERING,
        )
        offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.LIMIT,
        )
        plan = factories.PlanFactory(offering=offering)
        factories.PlanComponentFactory(
            plan=plan,
            component=offering_component,
        )

        old_limits = {offering_component.type: 50}
        new_limits = {offering_component.type: 100}

        resource = factories.ResourceFactory(
            offering=offering,
            project=self.project,
            plan=plan,
            limits=old_limits,
        )

        order = factories.OrderFactory(
            offering=offering,
            project=self.project,
            created_by=self.manager,
            type=OrderTypes.UPDATE,
            state=OrderStates.PENDING_PROVIDER,
            resource=resource,
            attributes=dict(old_limits=old_limits),
            limits=new_limits,
            plan=plan,
        )

        self.approve_order(self.fixture.owner, order)
        order.resource.refresh_from_db()

        self.assertEqual(order.resource.state, ResourceStates.OK)
        self.assertEqual(order.resource.limits, new_limits)
        self.assertEqual(order.resource.plan, plan)

    def test_when_terminate_order_with_basic_offering_is_approved_resource_is_marked_as_terminated(
        self,
    ):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=BASIC_OFFERING
        )
        resource = factories.ResourceFactory(offering=offering)
        order = factories.OrderFactory(
            offering=offering,
            project=self.project,
            created_by=self.manager,
            type=OrderTypes.TERMINATE,
            resource=resource,
            state=OrderStates.PENDING_PROVIDER,
        )
        self.approve_order(self.fixture.owner, order)
        order.refresh_from_db()
        self.assertEqual(order.resource.state, ResourceStates.TERMINATED)

    def test_when_order_with_basic_offering_is_approved_resource_is_marked_as_ok(self):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=BASIC_OFFERING
        )
        order = factories.OrderFactory(
            offering=offering,
            project=self.project,
            created_by=self.manager,
            state=OrderStates.PENDING_PROVIDER,
        )
        self.approve_order(self.fixture.owner, order)
        order.refresh_from_db()
        self.assertEqual(order.resource.state, ResourceStates.OK)

    def approve_order(self, user, order):
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(order, "approve_by_provider")
        response = self.client.post(url)
        order.refresh_from_db()
        return response


@ddt
class OrderRejectByConsumerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order: models.Order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.REJECT_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.REJECT_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.REJECT_ORDER)

    def reject_order(self, user, data=None):
        url = factories.OrderFactory.get_url(self.order, "reject_by_consumer")
        self.client.force_authenticate(user)
        return self.client.post(url, data=data)

    @data("staff", "manager", "admin", "owner")
    def test_authorized_user_can_reject_order(self, user):
        response = self.reject_order(getattr(self.fixture, user))

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.order.state, OrderStates.REJECTED)

    def test_support_users_can_not_reject_order(self):
        response = self.reject_order(self.fixture.global_support)
        self.client.force_authenticate()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_can_not_reject_reviewed_order(self):
        self.order.reject()
        self.order.save()
        response = self.reject_order(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_order_rejecting_is_not_available_for_blocked_organization(self):
        self.order.project.customer.blocked = True
        self.order.project.customer.save()
        response = self.reject_order(self.fixture.manager)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_error_details_are_saved_when_provided(self):
        error_data = {
            "error_message": "Test error message",
            "error_traceback": "Test stack trace",
        }
        response = self.reject_order(self.fixture.staff, data=error_data)

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.order.state, OrderStates.REJECTED)
        self.assertEqual(self.order.error_message, "Test error message")
        self.assertEqual(self.order.error_traceback, "Test stack trace")
        self.assertIsNotNone(self.order.error_updated_at)

    def test_empty_request_still_works(self):
        response = self.reject_order(self.fixture.staff)

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.order.state, OrderStates.REJECTED)
        self.assertEqual(self.order.error_message, "")
        self.assertEqual(self.order.error_traceback, "")

    def test_partial_error_data_works(self):
        error_data = {"error_message": "Only message provided"}
        response = self.reject_order(self.fixture.staff, data=error_data)

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.order.state, OrderStates.REJECTED)
        self.assertEqual(self.order.error_message, "Only message provided")
        self.assertEqual(self.order.error_traceback, "")

    def test_consumer_rejection_comment_is_saved(self):
        data = {"consumer_rejection_comment": "Budget not approved"}
        response = self.reject_order(self.fixture.staff, data=data)

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.order.state, OrderStates.REJECTED)
        self.assertEqual(self.order.consumer_rejection_comment, "Budget not approved")

    def test_consumer_rejection_comment_defaults_to_empty(self):
        response = self.reject_order(self.fixture.staff)

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.order.consumer_rejection_comment, "")


@ddt
class OrderRejectByProviderTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING, customer=self.fixture.customer
        )
        resource = factories.ResourceFactory(offering=self.offering)
        self.order = factories.OrderFactory(
            project=self.project,
            created_by=self.manager,
            resource=resource,
            offering=self.offering,
            state=OrderStates.PENDING_PROVIDER,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.REJECT_ORDER)

    @data(
        "staff",
        "owner",
    )
    def test_authorized_user_can_reject_order(self, user):
        response = self.reject_order(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.REJECTED)

    @data(
        "admin",
        "manager",
    )
    def test_user_cannot_reject_order(self, user):
        response = self.reject_order(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        OrderStates.CANCELED,
        OrderStates.EXECUTING,
    )
    def test_order_cannot_be_rejected_if_it_is_in_canceled_or_executing_state(
        self, state
    ):
        self.order.state = state
        self.order.save()
        response = self.reject_order("staff")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_when_create_order_with_basic_offering_is_rejected_resource_is_marked_as_terminated(
        self,
    ):
        self.offering.type = BASIC_OFFERING
        self.offering.save()

        self.reject_order("owner")
        self.order.refresh_from_db()
        self.assertEqual(ResourceStates.TERMINATED, self.order.resource.state)

    def test_when_update_order_with_basic_offering_is_rejected_resource_is_marked_as_erred(
        self,
    ):
        self.offering.type = BASIC_OFFERING
        self.offering.save()
        self.order.type = OrderTypes.UPDATE
        self.order.save()

        plan_period = factories.ResourcePlanPeriodFactory()
        old_plan = plan_period.plan
        old_plan.offering = self.offering
        old_plan.save()

        old_limits = {"unit": 50}
        resource = self.order.resource
        resource.plan = old_plan
        resource.limits = old_limits
        resource.save()

        plan_period.resource = resource
        plan_period.save()

        self.reject_order("owner")
        self.order.refresh_from_db()
        self.assertEqual(ResourceStates.OK, self.order.resource.state)
        self.assertEqual(old_plan, self.order.resource.plan)
        self.assertEqual(old_limits, self.order.resource.limits)

    def test_when_terminate_order_with_basic_offering_is_rejected_resource_is_marked_as_ok(
        self,
    ):
        self.offering.type = BASIC_OFFERING
        self.offering.save()
        self.order.type = OrderTypes.TERMINATE
        self.order.save()

        self.reject_order("owner")
        self.order.refresh_from_db()
        self.assertEqual(ResourceStates.OK, self.order.resource.state)

    def reject_order(self, user, data=None):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, "reject_by_provider")
        return self.client.post(url, data=data)

    def test_provider_rejection_comment_is_saved(self):
        data = {"provider_rejection_comment": "Insufficient resources available"}
        response = self.reject_order("owner", data=data)

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.order.state, OrderStates.REJECTED)
        self.assertEqual(
            self.order.provider_rejection_comment, "Insufficient resources available"
        )

    def test_empty_body_still_works_for_provider_rejection(self):
        response = self.reject_order("owner")

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.order.state, OrderStates.REJECTED)
        self.assertEqual(self.order.provider_rejection_comment, "")

    def test_provider_rejection_comment_visible_in_order_detail(self):
        data = {"provider_rejection_comment": "Cannot fulfill order"}
        self.reject_order("owner", data=data)

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OrderFactory.get_url(self.order)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["provider_rejection_comment"], "Cannot fulfill order"
        )


@ddt
class ApproveOrderAsProviderFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.order = self.fixture.order
        self.order.state = OrderStates.PENDING_PROVIDER
        self.order.save()

    def test_provider_owner_can_approve(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.assert_result("offering_owner", 1)

    def test_consumer_owner_can_not_approve(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.assert_result("owner", 0)

    def test_can_not_approve_executing_order(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.order.state = OrderStates.EXECUTING
        self.order.save()
        self.assert_result("offering_owner", 0)

    def assert_result(self, user, expected):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        response = self.client.get(url, {"can_approve_as_provider": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), expected)


@ddt
class ApproveOrderAsConsumerFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.order.state = OrderStates.PENDING_CONSUMER
        self.fixture.order.save()
        self.url = factories.OrderFactory.get_list_url()

    @data("offering_owner", "manager", "admin")
    def test_by_default_user_can_not_approve(self, user):
        self.assert_result(user, 0)

    def test_owner_can_get_order(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.assert_result("owner", 1)

    def test_manager_can_get_order(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.assert_result("manager", 1)

    def test_admin_can_get_order(self):
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.assert_result("admin", 1)

    def assert_result(self, user, expected):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        response = self.client.get(url, {"can_approve_as_consumer": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), expected)


class OrderApprovalByConsumerNotificationTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()

    @override_config(NOTIFY_STAFF_ABOUT_APPROVALS=True)
    def test_staff(self):
        self.check_notification(self.fixture.staff)

    def check_notification(self, user):
        structure_factories.NotificationFactory(
            key="marketplace.notify_consumer_about_pending_order"
        )
        tasks.notify_consumer_about_pending_order(self.fixture.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])

    def test_check_owner(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.check_notification(self.fixture.owner)

    def test_check_manager(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.check_notification(self.fixture.manager)

    def test_check_admin(self):
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.check_notification(self.fixture.admin)

    def test_notification_is_not_sent_when_there_are_no_approvers(self):
        tasks.notify_consumer_about_pending_order(self.fixture.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 0)


class OrderApprovalByProviderNotificationTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.order = self.fixture.order
        self.order.state = OrderStates.PENDING_PROVIDER
        self.order.save()

    def test_offering_owner(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.check_notification(self.fixture.offering_owner)

    def test_service_manager(self):
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.check_notification(self.fixture.service_manager)

    def check_notification(self, user):
        structure_factories.NotificationFactory(
            key="marketplace.notify_provider_about_pending_order"
        )
        tasks.notify_provider_about_pending_order(self.fixture.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])

    def test_notification_is_not_sent_when_there_are_no_approvers(self):
        tasks.notify_provider_about_pending_order(self.fixture.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 0)


class ScriptOfferingOrderReviewTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)

    def test_script_offering_auto_approves_by_default(self):
        """Test that SCRIPT_OFFERING orders skip provider approval by default."""
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=SCRIPT_OFFERING
        )
        order = factories.OrderFactory(
            offering=offering, project=self.project, created_by=self.manager
        )

        # Should return True (skip approval) by default
        result = utils.order_should_not_be_reviewed_by_provider(order)
        self.assertTrue(result)

    def test_script_offering_requires_approval_when_flag_is_false(self):
        """Test that SCRIPT_OFFERING orders require provider approval when auto_approve_marketplace_script=False."""
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=SCRIPT_OFFERING,
            plugin_options={"auto_approve_marketplace_script": False},
        )
        order = factories.OrderFactory(
            offering=offering, project=self.project, created_by=self.manager
        )

        # Should return False (require approval) when flag is False
        result = utils.order_should_not_be_reviewed_by_provider(order)
        self.assertFalse(result)

    def test_script_offering_requires_approval_for_service_provider_owner_when_disabled(
        self,
    ):
        """Test that service provider owners require approval when auto_approve_marketplace_script=False."""
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=SCRIPT_OFFERING,
            plugin_options={"auto_approve_marketplace_script": False},
        )
        order = factories.OrderFactory(
            offering=offering,
            project=self.project,
            created_by=self.fixture.owner,  # Owner is also the offering owner
        )

        # Should return False (require approval) even for service provider owner
        result = utils.order_should_not_be_reviewed_by_provider(order)
        self.assertFalse(result)

    def test_script_offering_requires_approval_for_staff_when_disabled(self):
        """Test that staff users require approval when auto_approve_marketplace_script=False."""
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=SCRIPT_OFFERING,
            plugin_options={"auto_approve_marketplace_script": False},
        )
        order = factories.OrderFactory(
            offering=offering,
            project=self.project,
            created_by=self.fixture.staff,
        )

        # Should return False (require approval) even for staff user
        result = utils.order_should_not_be_reviewed_by_provider(order)
        self.assertFalse(result)
