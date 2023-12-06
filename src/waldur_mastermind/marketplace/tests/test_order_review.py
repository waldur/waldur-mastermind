import datetime
from unittest import mock

from ddt import data, ddt
from django.core import mail
from django.test import override_settings
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import PLUGIN_NAME, models, tasks
from waldur_mastermind.marketplace.tasks import process_order
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace.tests.helpers import override_marketplace_settings


@ddt
class OrderApproveByConsumerTest(test.APITransactionTestCase):
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
        self.order.state = models.Order.States.EXECUTING
        self.order.save()
        response = self.approve_order(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.order.consumer_reviewed_by, None)

    def test_order_approving_is_not_available_for_blocked_organization(self):
        self.order.project.customer.blocked = True
        self.order.project.customer.save()
        response = self.approve_order(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_mastermind.marketplace.tasks.process_order.delay')
    def test_order_with_basic_offering_is_approved_by_consumer_it_is_pending_for_provider_review_too(
        self, mocked_delay
    ):
        mocked_delay.side_effect = process_order
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=PLUGIN_NAME
        )
        order = factories.OrderFactory(
            offering=offering, project=self.project, created_by=self.manager
        )
        self.approve_order(self.fixture.owner, order)
        order.refresh_from_db()
        self.assertEqual(order.state, models.Order.States.PENDING)

    def test_user_cannot_approve_order_if_project_is_expired(self):
        self.project.end_date = datetime.datetime(year=2020, month=1, day=1).date()
        self.project.save()

        with freeze_time('2020-01-01'):
            response = self.approve_order(self.fixture.staff)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def approve_order(self, user, order=None):
        order = order or self.order
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(order, 'approve_by_consumer')
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


@ddt
@override_settings(task_always_eager=True)
class OrderApproveByProviderTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)

    def test_when_update_order_with_basic_offering_is_approved_resource_is_marked_as_ok(
        self,
    ):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=PLUGIN_NAME,
        )
        offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=models.OfferingComponent.BillingTypes.LIMIT,
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
            type=models.Order.Types.UPDATE,
            resource=resource,
            attributes=dict(old_limits=old_limits),
            limits=new_limits,
            plan=plan,
        )

        self.approve_order(self.fixture.owner, order)
        order.resource.refresh_from_db()

        self.assertEqual(order.resource.state, models.Resource.States.OK)
        self.assertEqual(order.resource.limits, new_limits)
        self.assertEqual(order.resource.plan, plan)

    def test_when_terminate_order_with_basic_offering_is_approved_resource_is_marked_as_terminated(
        self,
    ):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=PLUGIN_NAME
        )
        resource = factories.ResourceFactory(offering=offering)
        order = factories.OrderFactory(
            offering=offering,
            project=self.project,
            created_by=self.manager,
            type=models.Order.Types.TERMINATE,
            resource=resource,
        )
        self.approve_order(self.fixture.owner, order)
        order.refresh_from_db()
        self.assertEqual(order.resource.state, models.Resource.States.TERMINATED)

    def test_when_order_with_basic_offering_is_approved_resource_is_marked_as_ok(self):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=PLUGIN_NAME
        )
        order = factories.OrderFactory(
            offering=offering, project=self.project, created_by=self.manager
        )
        self.approve_order(self.fixture.owner, order)
        order.refresh_from_db()
        self.assertEqual(order.resource.state, models.Resource.States.OK)

    def approve_order(self, user, order=None):
        order = order or self.order
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(order, 'approve_by_provider')
        response = self.client.post(url)
        order.refresh_from_db()
        return response


@ddt
class OrderRejectByConsumerTest(test.APITransactionTestCase):
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

    def reject_order(self, user):
        url = factories.OrderFactory.get_url(self.order, 'reject_by_consumer')
        self.client.force_authenticate(user)
        return self.client.post(url)

    @data('staff', 'manager', 'admin', 'owner')
    def test_authorized_user_can_reject_order(self, user):
        response = self.reject_order(getattr(self.fixture, user))

        self.order.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.order.state, models.Order.States.REJECTED)

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


@ddt
class OrderRejectByProviderTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.offering = factories.OfferingFactory(
            type=PLUGIN_NAME, customer=self.fixture.customer
        )
        resource = factories.ResourceFactory(offering=self.offering)
        self.order = factories.OrderFactory(
            project=self.project,
            created_by=self.manager,
            resource=resource,
            offering=self.offering,
            state=models.Order.States.PENDING,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.REJECT_ORDER)

    @data(
        'staff',
        'owner',
    )
    def test_authorized_user_can_reject_order(self, user):
        response = self.reject_order(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, models.Order.States.REJECTED)

    @data(
        'admin',
        'manager',
    )
    def test_user_cannot_reject_order(self, user):
        response = self.reject_order(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        models.Order.States.CANCELED,
        models.Order.States.EXECUTING,
    )
    def test_order_cannot_be_rejected_if_it_is_in_canceled_or_executing_state(
        self, state
    ):
        self.order.state = state
        self.order.save()
        response = self.reject_order('staff')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_when_create_order_with_basic_offering_is_rejected_resource_is_marked_as_terminated(
        self,
    ):
        self.offering.type = PLUGIN_NAME
        self.offering.save()

        self.reject_order('owner')
        self.order.refresh_from_db()
        self.assertEqual(models.Resource.States.TERMINATED, self.order.resource.state)

    def test_when_update_order_with_basic_offering_is_rejected_resource_is_marked_as_erred(
        self,
    ):
        self.offering.type = PLUGIN_NAME
        self.offering.save()
        self.order.type = models.Order.Types.UPDATE
        self.order.save()

        plan_period = factories.ResourcePlanPeriodFactory()
        old_plan = plan_period.plan
        old_plan.offering = self.offering
        old_plan.save()

        old_limits = {'unit': 50}
        resource = self.order.resource
        resource.plan = old_plan
        resource.limits = old_limits
        resource.save()

        plan_period.resource = resource
        plan_period.save()

        self.reject_order('owner')
        self.order.refresh_from_db()
        self.assertEqual(models.Resource.States.OK, self.order.resource.state)
        self.assertEqual(old_plan, self.order.resource.plan)
        self.assertEqual(old_limits, self.order.resource.limits)

    def test_when_terminate_order_with_basic_offering_is_rejected_resource_is_marked_as_ok(
        self,
    ):
        self.offering.type = PLUGIN_NAME
        self.offering.save()
        self.order.type = models.Order.Types.TERMINATE
        self.order.save()

        self.reject_order('owner')
        self.order.refresh_from_db()
        self.assertEqual(models.Resource.States.OK, self.order.resource.state)

    def reject_order(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, 'reject_by_provider')
        return self.client.post(url)


@ddt
class ApproveOrderAsProviderFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.order = self.fixture.order
        self.order.state = models.Order.States.PENDING
        self.order.save()

    def test_provider_owner_can_approve(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.assert_result('offering_owner', 1)

    def test_consumer_owner_can_not_approve(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.assert_result('owner', 0)

    def test_can_not_approve_executing_order(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.order.state = models.Order.States.EXECUTING
        self.order.save()
        self.assert_result('offering_owner', 0)

    def assert_result(self, user, expected):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        response = self.client.get(url, {'can_approve_as_provider': True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), expected)


@ddt
class ApproveOrderAsConsumerFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.order.state = models.Order.States.PENDING
        self.fixture.order.save()
        self.url = factories.OrderFactory.get_list_url()

    @data('offering_owner', 'manager', 'admin')
    def test_by_default_user_can_not_approve(self, user):
        self.assert_result(user, 0)

    def test_owner_can_get_order(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.assert_result('owner', 1)

    def test_manager_can_get_order(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.assert_result('manager', 1)

    def test_admin_can_get_order(self):
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.assert_result('admin', 1)

    def assert_result(self, user, expected):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        response = self.client.get(url, {'can_approve_as_consumer': True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), expected)


class OrderApprovalByConsumerNotificationTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()

    @override_marketplace_settings(NOTIFY_STAFF_ABOUT_APPROVALS=True)
    def test_staff(self):
        user = self.fixture.staff
        event_type = 'notify_consumer_about_pending_order'
        structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
        tasks.notify_consumer_about_pending_order(self.fixture.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])

    def check_notification(self, user, role: Role):
        role.add_permission(PermissionEnum.APPROVE_ORDER)
        role.add_permission(PermissionEnum.APPROVE_ORDER)
        event_type = 'notify_consumer_about_pending_order'
        structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
        tasks.notify_consumer_about_pending_order(self.fixture.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])

    def test_check_owner(self):
        self.check_notification(self.fixture.owner, CustomerRole.OWNER)

    def test_check_manager(self):
        self.check_notification(self.fixture.manager, ProjectRole.MANAGER)

    def test_check_admin(self):
        self.check_notification(self.fixture.admin, ProjectRole.ADMIN)

    def test_notification_is_not_sent_when_there_are_no_approvers(self):
        tasks.notify_consumer_about_pending_order(self.fixture.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 0)


class OrderApprovalByProviderNotificationTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.order = self.fixture.order
        self.order.state = models.Order.States.PENDING
        self.order.save()

    def test_owner_case(self):
        user = self.fixture.offering_owner
        event_type = 'notify_provider_about_pending_order'
        structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
        tasks.notify_provider_about_pending_order(self.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])

    def test_service_manager_case(self):
        event_type = 'notify_provider_about_pending_order'
        structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
        permission = factories.OfferingPermissionFactory(offering=self.order.offering)
        user = permission.user
        tasks.notify_provider_about_pending_order(self.order.uuid.hex)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
