import datetime
from unittest import mock
from unittest.mock import patch

from constance.test.unittest import override_config
from django.core import mail
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.registry import SupportedServices
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.enums import Units
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    BillingTypes,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
    RobotAccountStates,
)
from waldur_openstack.tests.fixtures import OpenStackFixture

from . import factories, fixtures


class CalculateUsageForCurrentMonthTest(test.APITestCase):
    def setUp(self):
        offering = factories.OfferingFactory()
        plan = factories.PlanFactory(offering=offering)
        resource = factories.ResourceFactory(offering=offering)
        category_component = factories.CategoryComponentFactory()
        self.offering_component = factories.OfferingComponentFactory(
            offering=offering,
            parent=category_component,
            billing_type=BillingTypes.USAGE,
        )
        factories.PlanComponentFactory(plan=plan, component=self.offering_component)
        plan_period = models.ResourcePlanPeriod.objects.create(
            resource=resource, plan=plan, start=timezone.now()
        )
        models.ComponentUsage.objects.create(
            resource=resource,
            component=self.offering_component,
            usage=10,
            date=timezone.now(),
            billing_period=core_utils.month_start(timezone.now()),
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


class NotificationTest(test.APITestCase):
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


class ResourceEndDateNotificationTest(test.APITestCase):
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


class TerminateResource(test.APITestCase):
    def setUp(self):
        fixture = structure_fixtures.UserFixture()
        self.user = fixture.staff
        offering = factories.OfferingFactory()
        self.resource = factories.ResourceFactory(offering=offering)
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.TERMINATE,
            state=OrderStates.EXECUTING,
        )

    @patch("waldur_mastermind.marketplace.utils.logger.info")
    def test_not_raise_exception_if_order_has_not_been_created(
        self, mock_logger: mock.Mock
    ):
        tasks.terminate_resource(
            core_utils.serialize_instance(self.resource),
            core_utils.serialize_instance(self.user),
        )
        mock_logger.assert_called_once_with(
            "Terminate order has not been created because other executing orders exist."
        )


class ProjectEndDateTest(test.APITestCase):
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
                    type=OrderTypes.TERMINATE,
                ).count()
            )
            order = models.Order.objects.get(
                resource=self.fixture.resource, type=OrderTypes.TERMINATE
            )
            # BASIC_OFFERING skips consumer review but not provider review
            # (order_should_not_be_reviewed_by_provider returns False for it),
            # so the order lands in PENDING_PROVIDER, not EXECUTING.
            self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)
            self.assertEqual(order.created_by, core_utils.get_system_robot())

    def test_terminate_resources_if_project_end_date_requested_by_cannot_approve_order(
        self,
    ):
        # Regression: project.end_date_requested_by is a distinct fallback from
        # resource.end_date_requested_by (utils.schedule_resources_termination)
        # and can equally be an ordinary project manager who lacks ORDER.APPROVE.
        # The order still gets created and lands in PENDING_CONSUMER, but
        # terminate_resource force-approves it immediately as the system robot
        # — created_by (the manager, for audit) stays untouched, only who
        # reviewed it changes.
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE)
        ProjectRole.MANAGER.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_PRIVATE_ORDER)

        self.fixture.project.end_date_requested_by = self.fixture.manager
        self.fixture.project.save()

        system_robot = core_utils.get_system_robot()

        with freeze_time("2020-01-02"):
            tasks.terminate_resources_if_project_end_date_has_been_reached()

        order = models.Order.objects.get(
            resource=self.fixture.resource, type=OrderTypes.TERMINATE
        )
        self.assertNotEqual(order.state, OrderStates.PENDING_CONSUMER)
        self.assertEqual(order.created_by, self.fixture.manager)
        self.assertEqual(order.consumer_reviewed_by, system_robot)

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


class GracePeriodPausingTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # Project end_date in the past, but grace period extends beyond today
        self.fixture.project.end_date = datetime.date(2020, 1, 1)
        self.fixture.project.grace_period_days = 30
        self.fixture.project.save()
        self.fixture.resource.set_state_ok()
        self.fixture.resource.save()

    @freeze_time("2020-01-15")
    def test_resource_is_paused_when_project_in_grace_period_and_offering_opts_in(self):
        self.fixture.offering.plugin_options = {"supports_pausing": True}
        self.fixture.offering.save()

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.fixture.resource.refresh_from_db()
        self.assertTrue(self.fixture.resource.paused)

    @freeze_time("2020-01-15")
    def test_resource_stays_active_when_offering_opts_out(self):
        self.fixture.offering.plugin_options = {"supports_pausing": False}
        self.fixture.offering.save()

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.fixture.resource.refresh_from_db()
        self.assertFalse(self.fixture.resource.paused)

    @freeze_time("2020-01-15")
    def test_already_paused_resource_is_not_saved_again(self):
        self.fixture.offering.plugin_options = {"supports_pausing": True}
        self.fixture.offering.save()
        self.fixture.resource.paused = True
        self.fixture.resource.save()

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.fixture.resource.refresh_from_db()
        self.assertTrue(self.fixture.resource.paused)

    @freeze_time("2020-02-01")
    def test_resources_terminated_after_grace_period_ends(self):
        """After effective_end_date passes, resources are terminated regardless of supports_pausing."""
        self.fixture.offering.plugin_options = {"supports_pausing": True}
        self.fixture.offering.save()

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.assertTrue(
            models.Order.objects.filter(
                resource=self.fixture.resource,
                type=OrderTypes.TERMINATE,
            ).exists()
        )


class GracePeriodDisableTest(test.APITestCase):
    """Offerings with plugin_options.disable_grace_period terminate on the
    project end date, ignoring the grace window, and are never paused."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # end_date in the past; grace period would extend termination to 2020-01-31
        self.fixture.project.end_date = datetime.date(2020, 1, 1)
        self.fixture.project.grace_period_days = 30
        self.fixture.project.save()
        self.fixture.resource.set_state_ok()
        self.fixture.resource.save()

    @freeze_time("2020-01-15")
    def test_grace_disabled_resource_is_terminated_within_grace_window(self):
        self.fixture.offering.plugin_options = {"disable_grace_period": True}
        self.fixture.offering.save()

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.assertTrue(
            models.Order.objects.filter(
                resource=self.fixture.resource,
                type=OrderTypes.TERMINATE,
            ).exists()
        )

    @freeze_time("2020-01-15")
    def test_normal_resource_is_not_terminated_within_grace_window(self):
        # Control: without the flag, the resource survives the grace window.
        self.fixture.offering.plugin_options = {}
        self.fixture.offering.save()

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.assertFalse(
            models.Order.objects.filter(
                resource=self.fixture.resource,
                type=OrderTypes.TERMINATE,
            ).exists()
        )

    @freeze_time("2020-01-15")
    def test_grace_disabled_resource_is_terminated_not_paused(self):
        # supports_pausing would normally pause during grace; disable_grace_period
        # takes precedence and the resource is terminated instead.
        self.fixture.offering.plugin_options = {
            "supports_pausing": True,
            "disable_grace_period": True,
        }
        self.fixture.offering.save()

        tasks.terminate_resources_if_project_end_date_has_been_reached()

        self.fixture.resource.refresh_from_db()
        self.assertFalse(self.fixture.resource.paused)
        self.assertTrue(
            models.Order.objects.filter(
                resource=self.fixture.resource,
                type=OrderTypes.TERMINATE,
            ).exists()
        )


class ResourceEndingNotificationGraceTest(test.APITestCase):
    """notification_about_resource_ending fires on the resource's effective end
    date, so grace-disabled resources (which terminate on the raw project end
    date, without an own end date) are included when the project has a grace
    window."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.resource.set_state_ok()
        self.fixture.resource.save()
        self.fixture.manager  # project user who receives the notification
        structure_factories.NotificationFactory(
            key="marketplace.notification_about_resource_ending"
        )

    def _configure(self, project_end_date, grace, plugin_options):
        self.fixture.project.end_date = project_end_date
        self.fixture.project.grace_period_days = grace
        self.fixture.project.save()
        self.fixture.offering.plugin_options = plugin_options
        self.fixture.offering.save()

    def test_grace_disabled_resource_notified_by_raw_project_end_date(self):
        # Project raw end date is 7 days out; grace-disabled → terminates then.
        self._configure(datetime.date(2020, 1, 8), 30, {"disable_grace_period": True})
        with freeze_time("2020-01-01"):
            tasks.notification_about_resource_ending()
        self.assertTrue(mail.outbox)
        self.assertIn(self.fixture.resource.name, mail.outbox[0].subject)

    def test_normal_resource_without_own_end_date_is_not_notified(self):
        # Normal offering with no own end date → the project-ending notice covers
        # it, so notification_about_resource_ending stays silent.
        self._configure(datetime.date(2020, 1, 8), 30, {})
        with freeze_time("2020-01-01"):
            tasks.notification_about_resource_ending()
        self.assertFalse(mail.outbox)

    def test_grace_disabled_without_grace_window_is_not_double_notified(self):
        # grace = 0 → raw == effective → project-ending notice covers it.
        self._configure(datetime.date(2020, 1, 8), 0, {"disable_grace_period": True})
        with freeze_time("2020-01-01"):
            tasks.notification_about_resource_ending()
        self.assertFalse(mail.outbox)

    def test_own_end_date_resource_is_still_notified(self):
        # Regression: the original behaviour (own end date approaching) is kept.
        self._configure(None, 0, {})
        self.fixture.resource.end_date = datetime.date(2020, 1, 8)
        self.fixture.resource.save()
        with freeze_time("2020-01-01"):
            tasks.notification_about_resource_ending()
        self.assertTrue(mail.outbox)
        self.assertIn(self.fixture.resource.name, mail.outbox[0].subject)


@override_config(ENABLE_STALE_RESOURCE_NOTIFICATIONS=True)
class NotificationAboutStaleResourceTest(test.APITestCase):
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
        item.unit = Units.QUANTITY
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


class ResourceEndDateTest(test.APITestCase):
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
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
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
                    resource=self.fixture.resource,
                    type=OrderTypes.TERMINATE,
                ).count()
            )
            order = models.Order.objects.get(
                resource=self.fixture.resource, type=OrderTypes.TERMINATE
            )
            # The fixture offering is BASIC_OFFERING, so consumer review is
            # skipped but provider review is not (order_should_not_be_reviewed_
            # by_provider returns False for BASIC_OFFERING) — the order lands
            # in PENDING_PROVIDER, not EXECUTING.
            self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)
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
                    resource=self.fixture.resource,
                    type=OrderTypes.TERMINATE,
                ).count()
            )
            order = models.Order.objects.get(
                resource=self.fixture.resource, type=OrderTypes.TERMINATE
            )
            self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)
            self.assertEqual(order.created_by, user)

    def test_terminate_resource_reuses_existing_pending_order_if_end_date_has_been_reached(
        self,
    ):
        # Regression: a project admin can request termination (RESOURCE.TERMINATE)
        # but cannot approve it (no ORDER.APPROVE) — mirrors production
        # PROJECT.ADMIN permissions (docker/rootfs/etc/waldur/permissions.yaml).
        # The order they submit before the resource's end_date must not be
        # cancelled and replaced once the end date is reached — it must be
        # approved in place, preserving who originally requested it.
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_RESOURCE)
        ProjectRole.ADMIN.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_PRIVATE_ORDER)

        with freeze_time("2019-12-01"):
            response = utils.terminate_resource(self.resource, self.fixture.admin)
            self.assertEqual(response.status_code, 200)

        order = models.Order.objects.get(
            resource=self.resource, type=OrderTypes.TERMINATE
        )
        self.assertEqual(order.state, OrderStates.PENDING_CONSUMER)
        order_pk = order.pk

        with freeze_time("2020-01-01"):
            tasks.terminate_expired_resources()

        order.refresh_from_db()
        self.assertEqual(order.pk, order_pk)
        self.assertNotEqual(order.state, OrderStates.PENDING_CONSUMER)
        self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)
        self.assertEqual(order.created_by, self.fixture.admin)
        self.assertEqual(order.consumer_reviewed_by, self.system_robot)

    def test_terminate_resource_if_end_date_requested_by_cannot_approve_order(self):
        # Regression: unlike test_terminate_resource_if_end_date_requested_by_is_passed
        # above, end_date_requested_by here is an ordinary project admin who lacks
        # ORDER.APPROVE. With no pre-existing pending order, the freshly created
        # order still lands in PENDING_CONSUMER, but terminate_resource notices
        # and force-approves it as the system robot in the same run — created_by
        # (the admin, for audit) is untouched, only who reviewed it changes.
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_RESOURCE)
        ProjectRole.ADMIN.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_PRIVATE_ORDER)

        with freeze_time("2020-01-01"):
            self.resource.end_date_requested_by = self.fixture.admin
            self.resource.save()

            self.assertTrue(self.resource.is_expired)
            tasks.terminate_expired_resources()
            self.resource.refresh_from_db()

            order = models.Order.objects.get(
                resource=self.fixture.resource, type=OrderTypes.TERMINATE
            )
            self.assertNotEqual(order.state, OrderStates.PENDING_CONSUMER)
            self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)
            self.assertEqual(order.created_by, self.fixture.admin)
            self.assertEqual(order.consumer_reviewed_by, self.system_robot)

    def test_each_expired_resource_is_attributed_independently(self):
        # Regression: actor for one resource must not leak into the next
        # iteration of the batch. The first resource has an explicit
        # end_date_requested_by; the second one does not. The second one
        # must be attributed to the system robot, not to the first user.
        end_date = datetime.datetime(day=1, month=1, year=2020).date()
        first_user = structure_factories.UserFactory(is_staff=True)
        self.resource.end_date_requested_by = first_user
        self.resource.save()

        second_resource = factories.ResourceFactory(
            offering=self.resource.offering,
            project=self.resource.project,
            end_date=end_date,
        )
        second_resource.set_state_ok()
        second_resource.save()

        with freeze_time("2020-01-01"):
            tasks.terminate_expired_resources()

        first_order = models.Order.objects.get(
            resource=self.resource, type=OrderTypes.TERMINATE
        )
        second_order = models.Order.objects.get(
            resource=second_resource, type=OrderTypes.TERMINATE
        )
        self.assertEqual(first_order.created_by, first_user)
        self.assertEqual(second_order.created_by, self.system_robot)

    def test_terminate_resource_when_end_date_requested_by_is_inactive(self):
        # Regression: if the user who requested the end date was later
        # deactivated, the actor must fall through to the system robot.
        # Otherwise the internal termination request is rejected with HTTP 401
        # "User inactive or deleted." and the resource is never terminated.
        with freeze_time("2020-01-01"):
            inactive_user = structure_factories.UserFactory(is_active=False)
            self.resource.end_date_requested_by = inactive_user
            self.resource.save()

            self.assertTrue(self.resource.is_expired)
            tasks.terminate_expired_resources()
            self.resource.refresh_from_db()

            order = models.Order.objects.get(
                resource=self.fixture.resource, type=OrderTypes.TERMINATE
            )
            self.assertEqual(order.created_by, self.system_robot)

    def test_notification_about_resource_ending(self):
        self.fixture.manager
        self.fixture.admin
        self.fixture.member

        with freeze_time("2019-12-25"):
            event_type = "notification_about_resource_ending"
            structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
            tasks.notification_about_resource_ending()

            self.assertEqual(len(mail.outbox), 3)
            subject = "Resource %s will be deleted." % self.resource.name
            self.assertEqual(mail.outbox[0].subject, subject)
            self.assertTrue(self.resource.uuid.hex in mail.outbox[0].body)


class MarkResourcesAsErredAfterTimeoutTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.offering = factories.OfferingFactory(
            scope=self.fixture.tenant, type=OPENSTACK_INSTANCE_OFFERING
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
        self.assertIsNotNone(self.order.error_updated_at)
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


class RemoveDeletedRobotAccountsTest(test.APITestCase):
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


class UpdateResourceScopeAvailabilityTest(test.APITestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()
        self.instance = self.fixture.instance
        self.offering = factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING, state=OfferingStates.ACTIVE
        )
        self.resource = factories.ResourceFactory(
            scope=self.instance, offering=self.offering
        )

    def test_update_scope_availability_when_offering_becomes_unavailable(self):
        self.instance.can_be_managed = True
        self.instance.save()
        tasks.update_resource_scope_availability(
            self.offering.uuid.hex, can_be_managed=False
        )
        self.instance.refresh_from_db()
        self.assertFalse(self.instance.can_be_managed)

    def test_update_scope_availability_when_offering_becomes_available(self):
        self.instance.can_be_managed = False
        self.instance.save()
        tasks.update_resource_scope_availability(
            self.offering.uuid.hex, can_be_managed=True
        )
        self.instance.refresh_from_db()
        self.assertTrue(self.instance.can_be_managed)


class ResetStuckUpdatingResourcesTest(test.APITestCase):
    """
    Test task that resets marketplace resources stuck in UPDATING state.

    This task handles the case where a resource remains in UPDATING state even
    though its related UPDATE order has been successfully completed.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

    def test_reset_stuck_resource_with_completed_update_order(self):
        """
        Test that a resource stuck in UPDATING state is reset to OK
        when its UPDATE order is completed (DONE).
        """
        # Set resource to UPDATING state
        self.resource.set_state_updating()
        self.resource.save()

        # Create a completed UPDATE order for the resource
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.UPDATE,
            state=OrderStates.DONE,
        )

        # Run the recovery task
        tasks.reset_stuck_updating_resources()

        # Verify the resource state is now OK
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)

    def test_do_not_reset_resource_with_executing_update_order(self):
        """
        Test that a resource in UPDATING state is NOT reset if its
        UPDATE order is still executing.
        """
        # Set resource to UPDATING state
        self.resource.set_state_updating()
        self.resource.save()

        # Create an executing UPDATE order for the resource
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.UPDATE,
            state=OrderStates.EXECUTING,
        )

        # Run the recovery task
        tasks.reset_stuck_updating_resources()

        # Verify the resource state is still UPDATING
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.UPDATING)

    def test_do_not_reset_resource_with_erred_update_order(self):
        """
        Test that a resource in UPDATING state is NOT reset if its
        UPDATE order has failed (ERRED).
        """
        # Set resource to UPDATING state
        self.resource.set_state_updating()
        self.resource.save()

        # Create a failed UPDATE order for the resource
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.UPDATE,
            state=OrderStates.ERRED,
        )

        # Run the recovery task
        tasks.reset_stuck_updating_resources()

        # Verify the resource state is still UPDATING
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.UPDATING)

    def test_do_not_reset_ok_resource(self):
        """
        Test that a resource already in OK state is not affected by the task.
        """
        # Set resource to OK state
        self.resource.set_state_ok()
        self.resource.save()

        # Create a completed UPDATE order for the resource
        factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.UPDATE,
            state=OrderStates.DONE,
        )

        # Run the recovery task
        tasks.reset_stuck_updating_resources()

        # Verify the resource state is still OK
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)

    def test_reset_only_latest_order_matters(self):
        """
        Test that only the latest UPDATE order state is considered.
        If the latest order is completed but there's an older executing order,
        the resource should still be reset.
        """
        # Set resource to UPDATING state
        self.resource.set_state_updating()
        self.resource.save()

        # Create an older executing UPDATE order
        older_order = factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.UPDATE,
            state=OrderStates.EXECUTING,
        )

        # Create a newer completed UPDATE order
        newer_order = factories.OrderFactory(
            resource=self.resource,
            type=OrderTypes.UPDATE,
            state=OrderStates.DONE,
        )

        # Ensure newer order is actually newer
        older_order.created = timezone.now() - datetime.timedelta(hours=1)
        older_order.save()
        newer_order.created = timezone.now()
        newer_order.save()

        # Run the recovery task
        tasks.reset_stuck_updating_resources()

        # Verify the resource state is now OK (based on latest order)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)

    def test_reset_stuck_resource_without_order_after_timeout(self):
        """
        Test that a resource stuck in UPDATING without any orders is reset
        after the timeout period (1 hour).
        """
        # Set resource to UPDATING state without any order (simulating backend sync operation)
        # Use update() to bypass auto_now on the modified field
        models.Resource.objects.filter(pk=self.resource.pk).update(
            state=ResourceStates.UPDATING,
            modified=timezone.now() - datetime.timedelta(hours=2),
        )

        # Run the recovery task
        tasks.reset_stuck_updating_resources()

        # Verify the resource state is now OK
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.OK)

    def test_do_not_reset_recently_stuck_resource_without_order(self):
        """
        Test that a resource stuck in UPDATING without any orders is NOT reset
        if it hasn't been stuck long enough (less than 1 hour).
        """
        # Set resource to UPDATING state without any order
        # Use update() to bypass auto_now on the modified field
        models.Resource.objects.filter(pk=self.resource.pk).update(
            state=ResourceStates.UPDATING,
            modified=timezone.now() - datetime.timedelta(minutes=30),
        )

        # Run the recovery task
        tasks.reset_stuck_updating_resources()

        # Verify the resource state is still UPDATING
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.UPDATING)


class MarketplaceAwareServiceListPullTaskTest(test.APITestCase):
    """The marketplace-aware ServiceListPullTask must skip ServiceSettings
    that no live offering references — otherwise stale settings (e.g. an
    old DigitalOcean credential left after the offering was deleted) keep
    generating periodic remote-API errors."""

    def setUp(self):
        registered_type = next(iter(SupportedServices.get_choices()))[0]
        common = dict(state=CoreStates.OK, is_active=True, type=registered_type)
        self.referenced = structure_factories.ServiceSettingsFactory(**common)
        self.orphan = structure_factories.ServiceSettingsFactory(**common)
        self.archived_only = structure_factories.ServiceSettingsFactory(**common)
        factories.OfferingFactory(scope=self.referenced, state=OfferingStates.ACTIVE)
        factories.OfferingFactory(
            scope=self.archived_only, state=OfferingStates.ARCHIVED
        )

    def test_only_settings_with_live_offering_are_pulled(self):
        result = list(tasks.ServiceResourcesListPullTask().get_pulled_objects())
        self.assertIn(self.referenced, result)
        self.assertNotIn(self.orphan, result)
        self.assertNotIn(self.archived_only, result)

    def test_paused_offering_keeps_settings_in_queryset(self):
        factories.OfferingFactory(scope=self.orphan, state=OfferingStates.PAUSED)
        result = list(tasks.ServicePropertiesListPullTask().get_pulled_objects())
        self.assertIn(self.orphan, result)
