import datetime
from unittest import mock

from ddt import data, ddt, unpack
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.marketplace import handlers, models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.utils import evaluate_usage_limit_restriction


def build_setup(
    action="pause",
    billing_type=BillingTypes.LIMIT,
    limit_period=LimitPeriods.MONTH,
    limit_amount=100,
):
    plugin_options = {"action_on_usage_limit": action} if action else {}
    offering = factories.OfferingFactory(plugin_options=plugin_options)
    component = factories.OfferingComponentFactory(
        offering=offering,
        type="cpu",
        billing_type=billing_type,
        limit_period=limit_period,
        limit_amount=limit_amount,
    )
    resource = factories.ResourceFactory(offering=offering, state=ResourceStates.OK)
    return offering, component, resource


def report(resource, component, usage, billing_period=None):
    if billing_period is None:
        billing_period = datetime.date.today().replace(day=1)
    return factories.ComponentUsageFactory(
        resource=resource,
        component=component,
        usage=usage,
        date=timezone.now(),
        billing_period=billing_period,
    )


class UsageLimitRestrictionCoreTest(test.APITestCase):
    def test_pause_applied_when_usage_reaches_limit(self):
        _, component, resource = build_setup(action="pause")
        report(resource, component, usage=100)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertTrue(resource.paused)
        self.assertFalse(resource.downscaled)
        self.assertEqual(resource.usage_limit_restriction, "paused")

    def test_pause_applied_when_usage_exceeds_limit(self):
        _, component, resource = build_setup(action="pause")
        report(resource, component, usage=150)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertTrue(resource.paused)

    def test_no_restriction_below_limit(self):
        _, component, resource = build_setup(action="pause")
        report(resource, component, usage=99)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused)
        self.assertEqual(resource.usage_limit_restriction, "")

    def test_downscale_applied_when_usage_reaches_limit(self):
        _, component, resource = build_setup(action="downscale")
        report(resource, component, usage=100)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertTrue(resource.downscaled)
        self.assertFalse(resource.paused)
        self.assertEqual(resource.usage_limit_restriction, "downscaled")

    def test_disabled_when_no_action_configured(self):
        _, component, resource = build_setup(action=None)
        report(resource, component, usage=1000)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused)
        self.assertFalse(resource.downscaled)
        self.assertEqual(resource.usage_limit_restriction, "")

    def test_component_without_limit_amount_is_ignored(self):
        _, component, resource = build_setup(action="pause", limit_amount=None)
        report(resource, component, usage=1000)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused)

    def test_usage_billing_type_component_does_not_trigger(self):
        # Only LIMIT components drive the restriction; a USAGE component with a
        # limit_amount must not.
        _, component, resource = build_setup(
            action="pause", billing_type=BillingTypes.USAGE
        )
        report(resource, component, usage=1000)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused)

    def test_skips_terminated_resource(self):
        _, component, resource = build_setup(action="pause")
        resource.state = ResourceStates.TERMINATED
        resource.save()
        report(resource, component, usage=100)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused)

    def test_auto_clear_when_usage_drops(self):
        _, component, resource = build_setup(action="pause")
        usage = report(resource, component, usage=100)
        evaluate_usage_limit_restriction(resource)
        resource.refresh_from_db()
        self.assertTrue(resource.paused)

        usage.usage = 10
        usage.save()
        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused)
        self.assertEqual(resource.usage_limit_restriction, "")

    def test_auto_clear_only_affects_own_restriction(self):
        # Simulate a restriction applied for another reason (e.g. staff pause):
        # paused is True but the marker is empty.
        _, component, resource = build_setup(action="pause")
        resource.paused = True
        resource.save()
        report(resource, component, usage=10)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertTrue(resource.paused)
        self.assertEqual(resource.usage_limit_restriction, "")

    def test_action_switch_moves_restriction(self):
        offering, component, resource = build_setup(action="pause")
        report(resource, component, usage=100)
        evaluate_usage_limit_restriction(resource)
        resource.refresh_from_db()
        self.assertTrue(resource.paused)

        offering.plugin_options = {"action_on_usage_limit": "downscale"}
        offering.save()
        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused)
        self.assertTrue(resource.downscaled)
        self.assertEqual(resource.usage_limit_restriction, "downscaled")


@ddt
class UsageLimitRestrictionPeriodTest(test.APITestCase):
    """Behaviour across all four limit periods.

    ``get_current_period_usage`` aggregates ComponentUsage over a different
    window per period, so each period is verified for in-window trigger,
    out-of-window exclusion, and rollover clearing.
    """

    # (limit_period, frozen "now", in-window billing_period, out-of-window billing_period)
    IN_WINDOW_CASES = (
        (
            LimitPeriods.MONTH,
            "2026-06-15",
            datetime.date(2026, 6, 1),
            datetime.date(2026, 5, 1),
        ),
        (
            LimitPeriods.QUARTERLY,
            "2026-05-15",
            datetime.date(2026, 4, 1),
            datetime.date(2026, 3, 1),
        ),
        (
            LimitPeriods.ANNUAL,
            "2026-06-15",
            datetime.date(2026, 1, 1),
            datetime.date(2025, 12, 1),
        ),
    )

    @data(*IN_WINDOW_CASES)
    @unpack
    def test_in_window_usage_triggers(self, period, now, in_window, _out):
        with freeze_time(now):
            _, component, resource = build_setup(action="pause", limit_period=period)
            report(resource, component, usage=100, billing_period=in_window)

            evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertTrue(resource.paused, f"{period} in-window should trigger")

    @data(*IN_WINDOW_CASES)
    @unpack
    def test_out_of_window_usage_does_not_trigger(self, period, now, _in, out_window):
        with freeze_time(now):
            _, component, resource = build_setup(action="pause", limit_period=period)
            report(resource, component, usage=100, billing_period=out_window)

            evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused, f"{period} out-of-window must not trigger")

    # (limit_period, frozen "now" when reported, frozen "now" after rollover)
    ROLLOVER_CASES = (
        (LimitPeriods.MONTH, "2026-06-15", "2026-07-15"),
        (LimitPeriods.QUARTERLY, "2026-05-15", "2026-08-15"),
        (LimitPeriods.ANNUAL, "2026-06-15", "2027-06-15"),
    )

    @data(*ROLLOVER_CASES)
    @unpack
    def test_restriction_clears_on_period_rollover(self, period, now, later):
        with freeze_time(now):
            _, component, resource = build_setup(action="pause", limit_period=period)
            report(
                resource,
                component,
                usage=100,
                billing_period=datetime.date.today().replace(day=1),
            )
            evaluate_usage_limit_restriction(resource)
            resource.refresh_from_db()
            self.assertTrue(resource.paused)

        with freeze_time(later):
            evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(
            resource.paused, f"{period} restriction should clear after rollover"
        )

    def test_total_period_does_not_clear_over_time(self):
        with freeze_time("2026-06-15"):
            _, component, resource = build_setup(
                action="pause", limit_period=LimitPeriods.TOTAL
            )
            report(
                resource, component, usage=100, billing_period=datetime.date(2026, 6, 1)
            )
            evaluate_usage_limit_restriction(resource)
            resource.refresh_from_db()
            self.assertTrue(resource.paused)

        # A year later, total usage still accumulates — restriction must remain.
        with freeze_time("2027-06-15"):
            evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertTrue(resource.paused)

    def test_total_period_clears_when_limit_raised(self):
        with freeze_time("2026-06-15"):
            offering, component, resource = build_setup(
                action="pause", limit_period=LimitPeriods.TOTAL, limit_amount=100
            )
            report(
                resource, component, usage=100, billing_period=datetime.date(2026, 6, 1)
            )
            evaluate_usage_limit_restriction(resource)
            resource.refresh_from_db()
            self.assertTrue(resource.paused)

            component.limit_amount = 1000
            component.save()
            evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused)


class UsageLimitRestrictionWiringTest(test.APITestCase):
    def test_usage_report_schedules_evaluation(self):
        _, component, resource = build_setup(action="pause")
        usage = report(resource, component, usage=100)

        with (
            mock.patch(
                "waldur_mastermind.marketplace.tasks."
                "evaluate_usage_limit_restriction_task.delay"
            ) as mock_delay,
            mock.patch(
                "waldur_mastermind.marketplace.handlers.transaction.on_commit",
                side_effect=lambda fn: fn(),
            ),
        ):
            handlers.evaluate_usage_limit_on_usage_report(
                sender=models.ComponentUsage, instance=usage, created=True
            )

        mock_delay.assert_called_once_with(resource.pk)

    def test_usage_report_does_not_schedule_when_disabled(self):
        _, component, resource = build_setup(action=None)
        usage = report(resource, component, usage=100)

        with mock.patch(
            "waldur_mastermind.marketplace.tasks."
            "evaluate_usage_limit_restriction_task.delay"
        ) as mock_delay:
            handlers.evaluate_usage_limit_on_usage_report(
                sender=models.ComponentUsage, instance=usage, created=True
            )

        mock_delay.assert_not_called()

    def test_usage_report_does_not_schedule_for_non_limit_component(self):
        _, component, resource = build_setup(
            action="pause", billing_type=BillingTypes.USAGE
        )
        usage = report(resource, component, usage=100)

        with mock.patch(
            "waldur_mastermind.marketplace.tasks."
            "evaluate_usage_limit_restriction_task.delay"
        ) as mock_delay:
            handlers.evaluate_usage_limit_on_usage_report(
                sender=models.ComponentUsage, instance=usage, created=True
            )

        mock_delay.assert_not_called()

    def test_raising_limit_amount_schedules_reevaluation(self):
        offering, component, resource = build_setup(action="pause")

        component.limit_amount = 500
        with (
            mock.patch(
                "waldur_mastermind.marketplace.tasks."
                "evaluate_usage_limit_restriction_task.delay"
            ) as mock_delay,
            mock.patch(
                "waldur_mastermind.marketplace.handlers.transaction.on_commit",
                side_effect=lambda fn: fn(),
            ),
        ):
            handlers.evaluate_usage_limit_on_component_change(
                sender=models.OfferingComponent, instance=component, created=False
            )

        mock_delay.assert_called_once_with(resource.pk)


class UsageLimitRestrictionPerResourceLimitTest(test.APITestCase):
    """The limit checked against is the per-resource limit (``resource.limits``),
    not just the offering component's ``limit_amount``."""

    def _setup(self, limit_amount=None, resource_limit=2, action="pause"):
        offering, component, resource = build_setup(
            action=action, limit_amount=limit_amount
        )
        resource.limits = {component.type: resource_limit}
        resource.save()
        return offering, component, resource

    def test_pause_on_per_resource_limit_without_component_limit_amount(self):
        _, component, resource = self._setup(limit_amount=None, resource_limit=2)
        report(resource, component, usage=3)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertTrue(resource.paused)
        self.assertEqual(resource.usage_limit_restriction, "paused")

    def test_per_resource_limit_takes_precedence_over_component_limit_amount(self):
        # Component allows 100, but this resource's own limit is 2.
        _, component, resource = self._setup(limit_amount=100, resource_limit=2)
        report(resource, component, usage=3)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertTrue(resource.paused)

    def test_no_pause_below_per_resource_limit(self):
        _, component, resource = self._setup(limit_amount=None, resource_limit=5)
        report(resource, component, usage=4)

        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused)
        self.assertEqual(resource.usage_limit_restriction, "")

    def test_auto_clear_when_usage_drops_below_per_resource_limit(self):
        _, component, resource = self._setup(limit_amount=None, resource_limit=2)
        usage = report(resource, component, usage=3)
        evaluate_usage_limit_restriction(resource)
        resource.refresh_from_db()
        self.assertTrue(resource.paused)

        usage.usage = 1
        usage.save()
        evaluate_usage_limit_restriction(resource)

        resource.refresh_from_db()
        self.assertFalse(resource.paused)
        self.assertEqual(resource.usage_limit_restriction, "")


class UsageLimitRestrictionResourceLimitWiringTest(test.APITestCase):
    def test_usage_report_schedules_with_only_per_resource_limit(self):
        _, component, resource = build_setup(action="pause", limit_amount=None)
        resource.limits = {component.type: 2}
        resource.save()
        usage = report(resource, component, usage=3)

        with (
            mock.patch(
                "waldur_mastermind.marketplace.tasks."
                "evaluate_usage_limit_restriction_task.delay"
            ) as mock_delay,
            mock.patch(
                "waldur_mastermind.marketplace.handlers.transaction.on_commit",
                side_effect=lambda fn: fn(),
            ),
        ):
            handlers.evaluate_usage_limit_on_usage_report(
                sender=models.ComponentUsage, instance=usage, created=True
            )

        mock_delay.assert_called_once_with(resource.pk)

    def test_usage_report_does_not_schedule_without_any_limit(self):
        _, component, resource = build_setup(action="pause", limit_amount=None)
        # resource.limits carries no entry for the component type.
        usage = report(resource, component, usage=3)

        with mock.patch(
            "waldur_mastermind.marketplace.tasks."
            "evaluate_usage_limit_restriction_task.delay"
        ) as mock_delay:
            handlers.evaluate_usage_limit_on_usage_report(
                sender=models.ComponentUsage, instance=usage, created=True
            )

        mock_delay.assert_not_called()

    def test_lowering_resource_limit_schedules_reevaluation(self):
        _, component, resource = build_setup(action="pause", limit_amount=None)
        resource.limits = {component.type: 10}
        resource.save()
        resource.limits = {component.type: 1}  # changed, unsaved

        with (
            mock.patch(
                "waldur_mastermind.marketplace.tasks."
                "evaluate_usage_limit_restriction_task.delay"
            ) as mock_delay,
            mock.patch(
                "waldur_mastermind.marketplace.handlers.transaction.on_commit",
                side_effect=lambda fn: fn(),
            ),
        ):
            handlers.evaluate_usage_limit_on_resource_limit_change(
                sender=models.Resource, instance=resource, created=False
            )

        mock_delay.assert_called_once_with(resource.pk)

    def test_resource_limit_change_does_not_schedule_when_disabled(self):
        _, component, resource = build_setup(action=None, limit_amount=None)
        resource.limits = {component.type: 1}

        with mock.patch(
            "waldur_mastermind.marketplace.tasks."
            "evaluate_usage_limit_restriction_task.delay"
        ) as mock_delay:
            handlers.evaluate_usage_limit_on_resource_limit_change(
                sender=models.Resource, instance=resource, created=False
            )

        mock_delay.assert_not_called()
