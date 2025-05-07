import datetime
from unittest.mock import patch

from constance.test.unittest import override_config
from django.core import mail
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import models, tasks
from waldur_mastermind.marketplace.enums import (
    OrderStates,
    ResourceStates,
    RobotAccountStates,
)
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE
from waldur_openstack.tests.fixtures import OpenStackFixture

from . import factories, fixtures


class CalculateUsageForCurrentMonthTest(test.APITransactionTestCase):
    def setUp(self):
        offering = factories.OfferingFactory()
        plan = factories.PlanFactory(offering=offering)
        resource = factories.ResourceFactory(offering=offering)
        category_component = factories.CategoryComponentFactory()
        self.offering_component = factories.OfferingComponentFactory(
            offering=offering,
            parent=category_component,
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
        )
        factories.PlanComponentFactory(plan=plan, component=self.offering_component)
        plan_period = models.ResourcePlanPeriod.objects.create(
            resource=resource, plan=plan, start=timezone.now()
        )
        models.ComponentUsage.objects.create(
            resource=resource,
            component=self.offering_component,
            usage=10,
            date=datetime.datetime.now(),
            billing_period=core_utils.month_start(datetime.datetime.now()),
            plan_period=plan_period,
        )

    def test_calculate_usage_if_category_component_is_set(self):
        tasks.calculate_usage_for_current_month()
        self.assertEqual(models.CategoryComponentUsage.objects.count(), 2)

    def test_calculate_usage_if_category_component_is_not_set(self):
        self.offering_component.parent = None
        self.offering_component.save()
        tasks.calculate_usage_for_current_month()
        self.assertEqual(models.CategoryComponentUsage.objects.count(), 0)


class NotificationTest(test.APITransactionTestCase):
    def test_notify_about_resource_change(self):
        project_fixture = structure_fixtures.ProjectFixture()
        admin = project_fixture.admin
        project = project_fixture.project
        resource = factories.ResourceFactory(project=project, name="Test resource")
        event_type = "marketplace_resource_create_succeeded"
        structure_factories.NotificationFactory(key=f"marketplace.{event_type}")

        tasks.notify_about_resource_change(
            event_type,
            {"resource_name": resource.name},
            resource.uuid,
        )
        self.assertEqual(len(mail.outbox), 1)
        subject_template_name = "{}/{}_subject.txt".format(
            "marketplace",
            "marketplace_resource_create_succeeded",
        )
        subject = core_utils.format_text(
            subject_template_name, {"resource_name": resource.name}
        )
        self.assertEqual(mail.outbox[0].subject, subject)
        self.assertEqual(mail.outbox[0].to[0], admin.email)
        self.assertTrue(resource.name in mail.outbox[0].body)
        self.assertTrue(resource.name in mail.outbox[0].subject)

    @patch("waldur_mastermind.marketplace.tasks.core_utils.broadcast_mail")
    def test_notify_user_that_order_been_rejected(self, mock_broadcast_mail):
        """
        Test that when an order is rejected, a notification is sent to the user who created the order.

        This test verifies:
        1. The notification task is called with the correct template
        2. The email is sent to the user who created the order
        3. The context contains the necessary information about the rejected order
        """
        fixture = fixtures.MarketplaceFixture()
        order = fixture.order

        # Call the task that sends the notification
        tasks.notify_user_that_order_been_rejected(order.uuid.hex)

        # Verify that broadcast_mail was called
        mock_broadcast_mail.assert_called_once(), "Notification email was not sent"

        # Verify the correct template was used
        self.assertEqual(
            mock_broadcast_mail.call_args[0][1],
            "notification_to_user_that_order_been_rejected",
            "Wrong email template was used for notification",
        )

        # Verify the email was sent to the user who created the order
        recipients = mock_broadcast_mail.call_args[0][3]
        self.assertEqual(len(recipients), 1, "There should be exactly one recipient")
        self.assertEqual(
            recipients[0],
            order.created_by.email,
            "Notification was not sent to the user who created the order",
        )

        # Verify the context contains the necessary information
        context = mock_broadcast_mail.call_args[0][2]
        self.assertIn("order", context, "Context is missing the order object")
        self.assertEqual(context["order"], order, "Context contains wrong order object")
        self.assertIn("order_url", context, "Context is missing the order URL")
        self.assertIn("site_name", context, "Context is missing the site name")
        self.assertIn("order_type", context, "Context is missing the order type")


class ResourceEndDateNotificationTest(test.APITransactionTestCase):
    def test_notify_about_resource_scheduled_termination(self):
        fixture = fixtures.MarketplaceFixture()
        admin = fixture.admin
        manager = fixture.manager
        event_type = "marketplace_resource_termination_scheduled"
        structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
        tasks.notify_about_resource_termination(
            fixture.resource.uuid,
            fixture.offering_owner.uuid,
        )
        recipients = {m.to[0] for m in mail.outbox}
        self.assertEqual(recipients, {admin.email, manager.email})
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(fixture.resource.name in mail.outbox[0].body)
        self.assertTrue(fixture.resource.name in mail.outbox[0].subject)

    def test_mail_is_not_sent_if_there_are_no_project_admin_or_manager(self):
        fixture = fixtures.MarketplaceFixture()
        tasks.notify_about_resource_termination(
            fixture.resource.uuid,
            fixture.offering_owner.uuid,
        )
        self.assertEqual(len(mail.outbox), 0)

    @patch("waldur_mastermind.marketplace.tasks.core_utils.broadcast_mail")
    def test_notification_uses_different_templates_for_staff_and_other_users(
        self, mock_broadcast_mail
    ):
        fixture = fixtures.MarketplaceFixture()
        tasks.notify_about_resource_termination(
            fixture.resource.uuid, fixture.offering_owner.uuid, False
        )
        mock_broadcast_mail.assert_called()
        self.assertEqual(
            mock_broadcast_mail.call_args[0][1],
            "marketplace_resource_termination_scheduled",
        )

        tasks.notify_about_resource_termination(
            fixture.resource.uuid, fixture.offering_owner.uuid, True
        )
        mock_broadcast_mail.assert_called()
        self.assertEqual(
            mock_broadcast_mail.call_args[0][1],
            "marketplace_resource_termination_scheduled_staff",
        )


class TerminateResource(test.APITransactionTestCase):
    def setUp(self):
        fixture = structure_fixtures.UserFixture()
        self.user = fixture.staff
        offering = factories.OfferingFactory()
        self.resource = factories.ResourceFactory(offering=offering)
        factories.OrderFactory(
            resource=self.resource,
            type=models.Order.Types.TERMINATE,
            state=OrderStates.EXECUTING,
        )

    @patch("waldur_mastermind.marketplace.utils.logger")
    def test_not_raise_exception_if_order_has_not_been_created(self, mock_logger):
        tasks.terminate_resource(
            core_utils.serialize_instance(self.resource),
            core_utils.serialize_instance(self.user),
        )
        mock_logger.info.assert_called_once_with(
            "Terminate order has not been created because other executing orders exist."
        )


class ProjectEndDateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.project.end_date = datetime.datetime(
            day=1, month=1, year=2020
        ).date()
        self.fixture.project.save()
        self.fixture.resource.set_state_ok()
        self.fixture.resource.save()
        self.fixture.manager
        self.fixture.owner

    def test_terminate_resources_if_project_end_date_has_been_reached(self):
        with freeze_time("2020-01-02"):
            tasks.terminate_resources_if_project_end_date_has_been_reached()
            self.assertTrue(
                models.Order.objects.filter(
                    resource=self.fixture.resource,
                    type=models.Order.Types.TERMINATE,
                ).count()
            )
            order = models.Order.objects.get(
                resource=self.fixture.resource, type=models.Order.Types.TERMINATE
            )
            self.assertTrue(order.state, OrderStates.EXECUTING)

    def test_notification_about_project_ending(self):
        project_2 = structure_factories.ProjectFactory(
            customer=self.fixture.customer,
            end_date=datetime.datetime(day=1, month=1, year=2020),
        )
        project_2.add_user(self.fixture.manager, ProjectRole.MANAGER)

        with freeze_time("2019-12-25"):
            event_type = "notification_about_project_ending"
            structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
            tasks.notification_about_project_ending()

            self.assertEqual(
                len(mail.outbox), 2
            )  # we only have 2 emails, not 4, because batch sending is used

            subject = "Your 2 projects will be deleted on 01/01/2020."
            self.assertEqual(mail.outbox[0].subject, subject)
            self.assertEqual(mail.outbox[1].subject, subject)

            self.assertEqual(
                {mail.outbox[0].to[0], mail.outbox[1].to[0]},
                {self.fixture.manager.email, self.fixture.owner.email},
            )

            self.assertTrue(self.fixture.project.uuid.hex in mail.outbox[0].body)
            self.assertTrue(project_2.uuid.hex in mail.outbox[0].body)
            self.assertTrue(self.fixture.project.uuid.hex in mail.outbox[1].body)
            self.assertTrue(project_2.uuid.hex in mail.outbox[1].body)

    def test_member_of_other_project_is_excluded(self):
        other_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        manager = structure_factories.UserFactory()
        other_project.add_user(manager, ProjectRole.MANAGER)

        with freeze_time("2019-12-25"):
            event_type = "notification_about_project_ending"
            structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
            tasks.notification_about_project_ending()

            self.assertEqual(len(mail.outbox), 2)
            self.assertEqual(
                {mail.outbox[0].to[0], mail.outbox[1].to[0]},
                {self.fixture.manager.email, self.fixture.owner.email},
            )

    @freeze_time("2020-01-02")
    def test_expired_project_is_deleted_if_there_are_no_active_resources(self):
        self.fixture.resource.set_state_terminated()
        self.fixture.resource.save()

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.fixture.project.refresh_from_db()
        self.assertTrue(self.fixture.project.is_removed)

    @freeze_time("2020-01-02")
    def test_expired_project_is_not_deleted_if_there_are_active_resources(self):
        tasks.terminate_resources_if_project_end_date_has_been_reached()
        self.fixture.project.refresh_from_db()

    @freeze_time("2020-01-02")
    def test_expired_project_is_not_deleted_if_there_are_terminating_resources(self):
        self.fixture.resource.set_state_terminating()
        self.fixture.resource.save()

        tasks.terminate_resources_if_project_end_date_has_been_reached()
        self.fixture.project.refresh_from_db()


@override_config(ENABLE_STALE_RESOURCE_NOTIFICATIONS=True)
class NotificationAboutStaleResourceTest(test.APITransactionTestCase):
    def setUp(self):
        project_fixture = structure_fixtures.ProjectFixture()
        self.owner = project_fixture.owner
        project = project_fixture.project
        self.resource = factories.ResourceFactory(
            project=project, name="Test resource", state=ResourceStates.OK
        )
        self.resource.offering.type = "Test.Type"
        self.resource.offering.save()

    def test_send_notify_if_stale_resource_exists(self):
        event_type = "notification_about_stale_resources"
        structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
        tasks.notify_about_stale_resource()
        self.assertEqual(len(mail.outbox), 1)
        subject_template_name = "{}/{}_subject.txt".format(
            "marketplace",
            "notification_about_stale_resources",
        )
        subject = core_utils.format_text(subject_template_name, {})
        self.assertEqual(mail.outbox[0].subject, subject)
        self.assertEqual(mail.outbox[0].to[0], self.owner.email)
        self.assertTrue(self.resource.name in mail.outbox[0].body)

    def test_do_not_send_notify_if_stale_resource_does_not_exists(self):
        item = invoices_factories.InvoiceItemFactory(resource=self.resource)
        item.unit_price = 10
        item.quantity = 10
        item.unit = invoices_models.InvoiceItem.Units.QUANTITY
        item.save()

        self.assertTrue(item.price)
        tasks.notify_about_stale_resource()
        self.assertEqual(len(mail.outbox), 0)

    def test_send_notify_if_related_invoice_item_has_not_price(self):
        item = invoices_factories.InvoiceItemFactory(resource=self.resource)
        event_type = "notification_about_stale_resources"
        structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
        item.unit_price = 0
        item.save()
        self.assertFalse(item.price)
        tasks.notify_about_stale_resource()
        self.assertEqual(len(mail.outbox), 1)

    def test_send_notify_only_for_resources_belonging_to_billable_offerings(self):
        self.resource.offering.billable = False
        self.resource.offering.save()
        tasks.notify_about_stale_resource()
        self.assertEqual(len(mail.outbox), 0)

    @override_config(ENABLE_STALE_RESOURCE_NOTIFICATIONS=False)
    def test_do_not_send_notify_if_configuration_is_false(self):
        tasks.notify_about_stale_resource()
        self.assertEqual(len(mail.outbox), 0)


class ResourceEndDateTest(test.APITransactionTestCase):
    def setUp(self):
        # We need create a system robot account because
        # account created in a migration does not exist when test is running
        self.system_robot = structure_factories.UserFactory(
            first_name="System",
            last_name="Robot",
            username="system_robot",
            description="Special user used for performing actions on behalf of Waldur.",
            is_staff=True,
            is_active=True,
        )
        self.fixtures = fixtures.MarketplaceFixture()
        self.resource = self.fixtures.resource
        self.resource.end_date = datetime.datetime(day=1, month=1, year=2020).date()
        self.resource.set_state_ok()
        self.resource.save()

    def test_terminate_resource_if_its_end_date_has_been_reached(self):
        with freeze_time("2020-01-01"):
            self.assertTrue(self.resource.is_expired)
            tasks.terminate_expired_resources()
            self.resource.refresh_from_db()

            self.assertTrue(
                models.Order.objects.filter(
                    resource=self.fixtures.resource,
                    type=models.Order.Types.TERMINATE,
                ).count()
            )
            order = models.Order.objects.get(
                resource=self.fixtures.resource, type=models.Order.Types.TERMINATE
            )
            self.assertTrue(order.state, OrderStates.EXECUTING)
            self.assertEqual(order.created_by, self.system_robot)

    def test_terminate_resource_if_end_date_requested_by_is_passed(self):
        with freeze_time("2020-01-01"):
            user = structure_factories.UserFactory(is_staff=True)
            self.resource.end_date_requested_by = user
            self.resource.save()

            self.assertTrue(self.resource.is_expired)
            tasks.terminate_expired_resources()
            self.resource.refresh_from_db()

            self.assertTrue(
                models.Order.objects.filter(
                    resource=self.fixtures.resource,
                    type=models.Order.Types.TERMINATE,
                ).count()
            )
            order = models.Order.objects.get(
                resource=self.fixtures.resource, type=models.Order.Types.TERMINATE
            )
            self.assertTrue(order.state, OrderStates.EXECUTING)
            self.assertEqual(order.created_by, user)

    def test_notification_about_resource_ending(self):
        self.fixtures.manager
        self.fixtures.admin
        self.fixtures.member

        with freeze_time("2019-12-25"):
            event_type = "notification_about_resource_ending"
            structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
            tasks.notification_about_resource_ending()

            self.assertEqual(len(mail.outbox), 3)
            subject = "Resource %s will be deleted." % self.resource.name
            self.assertEqual(mail.outbox[0].subject, subject)
            self.assertTrue(self.resource.uuid.hex in mail.outbox[0].body)


class MarkResourcesAsErredAfterTimeoutTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.offering = factories.OfferingFactory(
            scope=self.fixture.tenant, type=INSTANCE_TYPE
        )
        self.order = factories.OrderFactory(
            offering=self.offering,
            state=OrderStates.EXECUTING,
        )
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            scope=self.fixture.instance,
            state=ResourceStates.CREATING,
        )
        self.order.resource = self.resource
        self.order.save()

    def test_stale_orders_are_marked_as_failed(self):
        # Arrange
        now = timezone.now()
        with freeze_time(now - datetime.timedelta(hours=3)):
            self.order.modified = timezone.now()
            self.order.save()

        # Act
        tasks.mark_resources_as_erred_after_timeout()

        # Assert
        self.order.refresh_from_db()
        self.resource.refresh_from_db()
        self.fixture.instance.refresh_from_db()

        self.assertEqual(self.order.state, OrderStates.ERRED)
        self.assertEqual(self.order.error_message, "Execution has timed out.")
        self.assertEqual(self.resource.state, ResourceStates.ERRED)
        self.assertEqual(self.resource.backend_metadata["state"], "ERRED")
        self.assertEqual(self.fixture.instance.state, CoreStates.ERRED)

    def test_recent_orders_are_not_marked_as_failed(self):
        # Arrange
        now = timezone.now()
        with freeze_time(now - datetime.timedelta(hours=1)):
            self.order.modified = timezone.now()
            self.order.save()

        # Act
        tasks.mark_resources_as_erred_after_timeout()

        # Assert
        self.order.refresh_from_db()
        self.resource.refresh_from_db()
        self.fixture.instance.refresh_from_db()

        self.assertEqual(self.order.state, OrderStates.EXECUTING)
        self.assertNotEqual(self.resource.state, ResourceStates.ERRED)
        self.assertNotEqual(self.fixture.instance.state, CoreStates.ERRED)


class RemoveDeletedRobotAccountsTest(test.APITransactionTestCase):
    """
    Test daily task that removes deleted robot accounts from the database.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.robot_account = models.RobotAccount.objects.create(
            username="test-robot",
            state=RobotAccountStates.OK,
            resource=self.resource,
        )

    def test_remove_deleted_robot_accounts(self):
        """
        Test that robot accounts with state DELETED are removed from the database.
        """
        # Set robot account to DELETED state
        self.robot_account.state = RobotAccountStates.DELETED
        self.robot_account.save()

        # Call task to remove deleted robot accounts
        tasks.remove_deleted_robot_accounts()

        # Assert that robot account is removed from the database
        with self.assertRaises(models.RobotAccount.DoesNotExist):
            self.robot_account.refresh_from_db()

        self.assertIsNone(
            models.RobotAccount.objects.filter(
                uuid=self.robot_account.uuid.hex
            ).first(),
            f"Robot account {self.robot_account.uuid.hex} should not exist",
        )

    def test_do_not_remove_active_robot_accounts(self):
        """
        Test that robot accounts with other states, for example REQUESTED, are not removed from the database.
        """
        # Set robot account to OK state
        self.robot_account.state = RobotAccountStates.REQUESTED
        self.robot_account.save()

        # Call task to remove deleted robot accounts
        tasks.remove_deleted_robot_accounts()

        # Assert that robot account is not removed from the database
        self.robot_account.refresh_from_db()
        self.assertEqual(
            self.robot_account.state,
            RobotAccountStates.REQUESTED,
            f"Robot account {self.robot_account.uuid.hex} should not be removed from the database",
        )
