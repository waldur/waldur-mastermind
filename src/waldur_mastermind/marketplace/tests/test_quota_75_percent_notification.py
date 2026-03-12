import datetime
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.utils import timezone
from rest_framework.test import APITestCase

from waldur_core.core import utils as core_utils
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import tasks
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories


class Quota75PercentNotificationHandlerTest(APITestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory()
        self.resource = factories.ResourceFactory(offering=self.offering)
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu",
            name="GPU",
            billing_type=BillingTypes.LIMIT,
        )
        self.resource.limits = {"gpu": 100}
        self.resource.save()

    @patch("waldur_mastermind.marketplace.tasks.notify_quota_75_percent.delay")
    def test_notification_dispatched_when_usage_crosses_75_percent(self, mock_delay):
        """Notification is triggered when usage reaches 75% of the limit for the first time."""
        with self.captureOnCommitCallbacks(execute=True):
            factories.ComponentUsageFactory(
                resource=self.resource,
                component=self.component,
                usage=75,
                billing_period=core_utils.month_start(timezone.now()),
            )
        mock_delay.assert_called_once_with(
            core_utils.serialize_instance(self.resource),
            core_utils.serialize_instance(self.component),
            "75",
        )

    @patch("waldur_mastermind.marketplace.tasks.notify_quota_75_percent.delay")
    def test_notification_not_sent_when_below_75_percent(self, mock_delay):
        """No notification when usage is below 75% of the limit."""
        with self.captureOnCommitCallbacks(execute=True):
            factories.ComponentUsageFactory(
                resource=self.resource,
                component=self.component,
                usage=74,
                billing_period=core_utils.month_start(timezone.now()),
            )
        mock_delay.assert_not_called()

    @patch("waldur_mastermind.marketplace.tasks.notify_quota_75_percent.delay")
    def test_notification_not_sent_twice_when_already_at_75_percent(self, mock_delay):
        """No duplicate notification when usage was already at or above 75% before update."""
        with self.captureOnCommitCallbacks(execute=True):
            usage = factories.ComponentUsageFactory(
                resource=self.resource,
                component=self.component,
                usage=75,
                billing_period=core_utils.month_start(timezone.now()),
            )
        mock_delay.reset_mock()
        # Update usage — still at threshold, should not fire again
        with self.captureOnCommitCallbacks(execute=True):
            usage.usage = Decimal("75")
            usage.save()
        mock_delay.assert_not_called()

    @patch("waldur_mastermind.marketplace.tasks.notify_quota_75_percent.delay")
    def test_notification_not_sent_for_non_limit_component(self, mock_delay):
        """Non-LIMIT billing type components do not trigger quota 75% notification."""
        usage_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            name="CPU",
            billing_type=BillingTypes.USAGE,
        )
        with self.captureOnCommitCallbacks(execute=True):
            factories.ComponentUsageFactory(
                resource=self.resource,
                component=usage_component,
                usage=1000,
                billing_period=core_utils.month_start(timezone.now()),
            )
        mock_delay.assert_not_called()

    @patch("waldur_mastermind.marketplace.tasks.notify_quota_75_percent.delay")
    def test_notification_not_sent_when_no_limit_set(self, mock_delay):
        """No notification when resource has no limit configured for the component."""
        component_no_limit = factories.OfferingComponentFactory(
            offering=self.offering,
            type="storage",
            name="Storage",
            billing_type=BillingTypes.LIMIT,
        )
        # resource.limits does not contain "storage"
        with self.captureOnCommitCallbacks(execute=True):
            factories.ComponentUsageFactory(
                resource=self.resource,
                component=component_no_limit,
                usage=100,
                billing_period=core_utils.month_start(timezone.now()),
            )
        mock_delay.assert_not_called()

    @patch("waldur_mastermind.marketplace.tasks.notify_quota_75_percent.delay")
    def test_notification_for_total_period_sums_all_usages(self, mock_delay):
        """For TOTAL limit_period, usages across all billing periods are summed."""
        total_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="node_hours",
            name="Node hours",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        self.resource.limits = {"node_hours": 100}
        self.resource.save()

        plan_period = factories.ResourcePlanPeriodFactory(
            resource=self.resource,
            plan=self.resource.plan,
            start=core_utils.month_start(timezone.now()),
        )
        # First usage: 50 (below 75 threshold)
        with self.captureOnCommitCallbacks(execute=True):
            factories.ComponentUsageFactory(
                resource=self.resource,
                component=total_component,
                usage=50,
                billing_period=core_utils.month_start(timezone.now()),
                plan_period=plan_period,
            )
        mock_delay.assert_not_called()

        # Second usage in a different billing period: 25 more -> total = 75 (at threshold)
        next_month = core_utils.month_start(timezone.now()) + datetime.timedelta(
            days=32
        )
        with self.captureOnCommitCallbacks(execute=True):
            factories.ComponentUsageFactory(
                resource=self.resource,
                component=total_component,
                usage=25,
                billing_period=core_utils.month_start(next_month),
                plan_period=plan_period,
            )
        mock_delay.assert_called_once_with(
            core_utils.serialize_instance(self.resource),
            core_utils.serialize_instance(total_component),
            "75.00",
        )

    @patch("waldur_mastermind.marketplace.tasks.notify_quota_75_percent.delay")
    def test_notification_not_sent_twice_for_total_period(self, mock_delay):
        """No duplicate notification when TOTAL period sum was already at threshold."""
        total_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="node_hours",
            name="Node hours",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        self.resource.limits = {"node_hours": 100}
        self.resource.save()

        plan_period = factories.ResourcePlanPeriodFactory(
            resource=self.resource,
            plan=self.resource.plan,
            start=core_utils.month_start(timezone.now()),
        )
        # Month 1: 50 units — below threshold, no notification
        with self.captureOnCommitCallbacks(execute=True):
            factories.ComponentUsageFactory(
                resource=self.resource,
                component=total_component,
                usage=50,
                billing_period=core_utils.month_start(timezone.now()),
                plan_period=plan_period,
            )
        mock_delay.assert_not_called()

        # Month 2: 25 more units — total hits threshold, notification fires once
        next_month = core_utils.month_start(timezone.now()) + datetime.timedelta(
            days=32
        )
        with self.captureOnCommitCallbacks(execute=True):
            factories.ComponentUsageFactory(
                resource=self.resource,
                component=total_component,
                usage=25,
                billing_period=core_utils.month_start(next_month),
                plan_period=plan_period,
            )
        mock_delay.assert_called_once()
        mock_delay.reset_mock()

        # Month 3: total already >= threshold → no second notification
        month_3 = core_utils.month_start(timezone.now()) + datetime.timedelta(days=64)
        with self.captureOnCommitCallbacks(execute=True):
            factories.ComponentUsageFactory(
                resource=self.resource,
                component=total_component,
                usage=10,
                billing_period=core_utils.month_start(month_3),
                plan_period=plan_period,
            )
        mock_delay.assert_not_called()


class Quota75PercentNotificationTaskTest(APITestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory()
        self.resource = factories.ResourceFactory(
            offering=self.offering, limits={"gpu": 100}
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu",
            name="GPU",
            billing_type=BillingTypes.LIMIT,
            measured_unit="GPU hours",
        )
        self.admin = structure_factories.UserFactory()
        self.manager = structure_factories.UserFactory()
        self.resource.project.add_user(self.admin, ProjectRole.ADMIN)
        self.resource.project.add_user(self.manager, ProjectRole.MANAGER)
        structure_factories.NotificationFactory(
            key="marketplace.notification_quota_75_percent"
        )

    def test_sends_email_to_admins_and_managers(self):
        """Task sends email to all project admins and managers."""
        tasks.notify_quota_75_percent(
            core_utils.serialize_instance(self.resource),
            core_utils.serialize_instance(self.component),
            75,
        )
        recipients = {m.to[0] for m in mail.outbox}
        self.assertIn(self.admin.email, recipients)
        self.assertIn(self.manager.email, recipients)

    def test_email_contains_resource_name(self):
        """Email body contains the resource name."""
        tasks.notify_quota_75_percent(
            core_utils.serialize_instance(self.resource),
            core_utils.serialize_instance(self.component),
            75,
        )
        self.assertTrue(len(mail.outbox) > 0)
        self.assertIn(self.resource.name, mail.outbox[0].body)

    def test_email_subject_contains_75_percent(self):
        """Email subject contains '75%'."""
        tasks.notify_quota_75_percent(
            core_utils.serialize_instance(self.resource),
            core_utils.serialize_instance(self.component),
            75,
        )
        self.assertTrue(len(mail.outbox) > 0)
        self.assertIn("75%", mail.outbox[0].subject)

    @patch("waldur_mastermind.marketplace.tasks.core_utils.broadcast_mail")
    def test_uses_correct_template(self, mock_broadcast_mail):
        """Task uses the notification_quota_75_percent template."""
        tasks.notify_quota_75_percent(
            core_utils.serialize_instance(self.resource),
            core_utils.serialize_instance(self.component),
            75,
        )
        call_args = mock_broadcast_mail.call_args[0]
        self.assertEqual(call_args[1], "notification_quota_75_percent")

    @patch("waldur_mastermind.marketplace.tasks.core_utils.broadcast_mail")
    def test_context_contains_required_fields(self, mock_broadcast_mail):
        """Task context contains all required fields."""
        tasks.notify_quota_75_percent(
            core_utils.serialize_instance(self.resource),
            core_utils.serialize_instance(self.component),
            75,
        )
        call_args = mock_broadcast_mail.call_args[0]
        context = call_args[2]
        self.assertEqual(context["project_name"], self.resource.project.name)
        self.assertEqual(context["resource_name"], self.resource.name)
        self.assertEqual(context["component_name"], self.component.name)
        self.assertEqual(context["component_type"], self.component.type)
        self.assertEqual(context["measured_unit"], self.component.measured_unit)
        self.assertEqual(context["allocation_total"], "100")
        self.assertIn("current_usage", context)
        self.assertIn("usage_percentage", context)
        self.assertIn("resource_url", context)
        self.assertIn("provider_name", context)
        self.assertIn("site_name", context)
        self.assertIn("user", context)

    @patch("waldur_mastermind.marketplace.tasks.core_utils.broadcast_mail")
    def test_no_email_sent_when_no_recipients(self, mock_broadcast_mail):
        """Task does not send email when no admin or manager users exist."""
        resource = factories.ResourceFactory(
            offering=self.offering, limits={"gpu": 100}
        )
        tasks.notify_quota_75_percent(
            core_utils.serialize_instance(resource),
            core_utils.serialize_instance(self.component),
            75,
        )
        mock_broadcast_mail.assert_not_called()

    @patch("waldur_mastermind.marketplace.tasks.core_utils.broadcast_mail")
    def test_handles_missing_resource(self, mock_broadcast_mail):
        """Task exits gracefully when resource does not exist."""
        tasks.notify_quota_75_percent(
            "marketplace.resource:999999", self.component.uuid.hex, 75
        )
        mock_broadcast_mail.assert_not_called()

    @patch("waldur_mastermind.marketplace.tasks.core_utils.broadcast_mail")
    def test_handles_missing_component(self, mock_broadcast_mail):
        """Task exits gracefully when component does not exist."""
        tasks.notify_quota_75_percent(
            core_utils.serialize_instance(self.resource),
            "marketplace.offeringcomponent:999999",
            75,
        )
        mock_broadcast_mail.assert_not_called()
