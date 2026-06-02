"""
Tests for HPCMP-453: Monthly reset for limit-based resources.

Verifies that at a monthly period boundary:
1. Raw usage IS reset (SLURM RawUsage=0 command sent)
2. Slowdown/paused state IS cleared
3. QoS IS updated accordingly on SLURM
"""

import datetime
from decimal import Decimal
from unittest.mock import patch

from django.db.models import signals
from django.test import TestCase
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing_usage import (
    schedule_component_usage_billing,
)
from waldur_mastermind.marketplace.handlers import process_billing_on_resource_save
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.policy import models, tasks
from waldur_mastermind.policy.tests.factories import SlurmPeriodicUsagePolicyFactory


def _disconnect_billing_signals():
    """Disconnect billing signals that conflict with freeze_time."""
    signals.post_save.disconnect(
        process_billing_on_resource_save,
        sender=marketplace_models.Resource,
        dispatch_uid="waldur_mastermind.marketplace.process_billing_on_resource_save",
    )
    signals.post_save.disconnect(
        schedule_component_usage_billing,
        sender=marketplace_models.ComponentUsage,
        dispatch_uid="waldur_mastermind.marketplace.schedule_component_usage_billing",
    )


def _reconnect_billing_signals():
    """Reconnect billing signals after tests."""
    signals.post_save.connect(
        process_billing_on_resource_save,
        sender=marketplace_models.Resource,
        dispatch_uid="waldur_mastermind.marketplace.process_billing_on_resource_save",
    )
    signals.post_save.connect(
        schedule_component_usage_billing,
        sender=marketplace_models.ComponentUsage,
        dispatch_uid="waldur_mastermind.marketplace.schedule_component_usage_billing",
    )


class MonthlyPeriodResetTest(TestCase):
    """Test HPCMP-453: monthly reset for limit-based resources.

    Scenario:
    - A monthly limit offering (e.g., "Errigal on Alps monthly limit (mng0r)")
    - A resource that exceeded its monthly allocation in February (paused + downscaled)
    - On March 1st, the new billing period should:
      1. Reset usage to 0 for the new month
      2. Clear paused/downscaled state
      3. Send apply_periodic_settings with reset_raw_usage=true to site agent
    """

    def setUp(self):
        # Disconnect billing signals - they conflict with freeze_time
        # (FakeDatetime vs FakeDate comparison) and are not relevant here.
        _disconnect_billing_signals()

        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = "Marketplace.Slurm"
        self.offering.save()

        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            name="Node hours",
            billing_type=marketplace_models.BillingTypes.USAGE,
        )

        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            name="cscs-cscs-dwdi-2-alps-errigal-mng0r",
            backend_id="cscs-dwdi-2-2",
            limits={"node": 1000},
        )

        marketplace_models.PlanComponent.objects.get_or_create(
            plan=self.resource.plan,
            component=self.component,
            defaults={"amount": 1000, "price": 1},
        )

        # Create a MONTHLY policy (period=MONTH_1)
        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners,request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.15,
            carryover_enabled=False,
            limit_type="GrpTRESMins",
            tres_billing_enabled=True,
            raw_usage_reset=True,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )

        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )

    def tearDown(self):
        _reconnect_billing_signals()

    def _create_component_usage(self, resource, component, usage_amount, billing_date):
        """Helper to create ComponentUsage for a specific billing period."""
        plan_period, _ = marketplace_models.ResourcePlanPeriod.objects.get_or_create(
            resource=resource,
            plan=resource.plan,
            start=billing_date,
        )
        return marketplace_models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            usage=Decimal(str(usage_amount)),
            date=billing_date,
            billing_period=billing_date,
            plan_period=plan_period,
        )

    @freeze_time("2026-02-15")
    def test_resource_gets_paused_when_exceeding_monthly_limit(self):
        """Verify that a resource is paused when usage exceeds limit within a month."""
        # Create February usage at 130% (exceeds grace limit of 115%)
        self._create_component_usage(
            self.resource,
            self.component,
            1300,
            datetime.date(2026, 2, 1),
        )

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled)
        self.assertTrue(self.resource.paused)

    @freeze_time("2026-03-03")
    def test_current_period_returns_monthly_for_monthly_policy(self):
        """_get_current_period() returns a monthly period for MONTH_1 policies."""
        current_period = self.policy._get_current_period()

        # The period format doesn't matter as long as the date range only
        # covers the current month (March 2026), not the whole quarter.
        date_range = self.policy._get_period_date_range(current_period)
        self.assertIsNotNone(date_range, "Period date range should be parseable")
        start_date, end_date = date_range

        # For a monthly policy on March 3rd, the period should cover only March
        self.assertEqual(start_date.month, 3, "Period should start in March")
        self.assertEqual(start_date.year, 2026)
        self.assertLessEqual(
            (end_date - start_date).days,
            31,
            "Monthly period should span at most 31 days",
        )

    @freeze_time("2026-03-03")
    def test_february_usage_not_counted_in_march_for_monthly_policy(self):
        """Usage from February is NOT included in March's period for monthly policies."""
        # February usage: 1300 node-hours (exceeded limit)
        self._create_component_usage(
            self.resource,
            self.component,
            1300,
            datetime.date(2026, 2, 1),
        )

        # March usage: 0 (new month, no usage yet)
        usage_pct = self.policy.get_resource_usage_percentage(self.resource)

        self.assertAlmostEqual(
            usage_pct,
            0.0,
            places=1,
            msg=f"Usage should be 0% in new month (March) but got {usage_pct}%.",
        )

    @freeze_time("2026-03-03")
    def test_paused_state_cleared_at_monthly_boundary(self):
        """Resource paused in February is un-paused in March when usage resets."""
        # February: resource was paused due to exceeding limit
        self._create_component_usage(
            self.resource,
            self.component,
            1300,
            datetime.date(2026, 2, 1),
        )
        self.resource.paused = True
        self.resource.downscaled = True
        self.resource.save()

        # March 3rd: evaluate resource - should clear paused state
        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        self.resource.refresh_from_db()

        self.assertFalse(
            self.resource.downscaled,
            "Resource should not be downscaled in new month with 0 usage",
        )
        self.assertFalse(
            self.resource.paused,
            "Resource should not be paused in new month with 0 usage",
        )

    @freeze_time("2026-03-03")
    def test_stomp_message_sent_at_period_boundary(self):
        """apply_policy_actions is called at period boundary to send reset_raw_usage."""
        # February: resource was paused
        self._create_component_usage(
            self.resource,
            self.component,
            1300,
            datetime.date(2026, 2, 1),
        )
        self.resource.paused = True
        self.resource.downscaled = True
        self.resource.save()

        with patch.object(
            models.SlurmPeriodicUsagePolicy,
            "apply_policy_actions",
            return_value=True,
        ) as mock_apply:
            tasks.evaluate_resource_against_policy(
                str(self.resource.uuid), str(self.policy.uuid)
            )

            self.assertTrue(
                mock_apply.called,
                "apply_policy_actions should be called at period boundary "
                "to send reset_raw_usage STOMP message.",
            )

    @freeze_time("2026-03-03")
    def test_reset_raw_usage_in_settings_at_period_boundary(self):
        """SLURM settings at period boundary include reset_raw_usage=true."""
        settings = self.policy.calculate_slurm_settings(self.resource)
        self.assertTrue(
            settings.get("reset_raw_usage"),
            "Settings should include reset_raw_usage=true for period boundary reset",
        )


class MonthlyPeriodBoundaryTaskTest(TestCase):
    """Test that a periodic task exists to handle period boundary resets."""

    def setUp(self):
        _disconnect_billing_signals()

        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = "Marketplace.Slurm"
        self.offering.save()

        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            name="Node hours",
            billing_type=marketplace_models.BillingTypes.USAGE,
        )

        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            backend_id="test-account",
            limits={"node": 1000},
        )

        marketplace_models.PlanComponent.objects.get_or_create(
            plan=self.resource.plan,
            component=self.component,
            defaults={"amount": 1000, "price": 1},
        )

        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.15,
            carryover_enabled=False,
            raw_usage_reset=True,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )

        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )

    def tearDown(self):
        _reconnect_billing_signals()

    def test_period_boundary_reset_task_exists(self):
        """A Celery task exists to handle period boundary resets."""
        from waldur_mastermind.policy.extension import PolicyExtension

        celery_tasks = PolicyExtension.celery_tasks()
        task_names = [t.get("task", "") for t in celery_tasks.values()]

        has_period_reset_task = any(
            "period" in name.lower() or "reset" in name.lower() for name in task_names
        )
        self.assertTrue(
            has_period_reset_task,
            f"No period reset task found in celery_tasks: {task_names}.",
        )

    @freeze_time("2026-03-01")
    def test_period_boundary_resets_all_paused_resources(self):
        """At period boundary, all paused resources for monthly policies are reset."""
        # Set up: resource paused at end of February
        self.resource.paused = True
        self.resource.downscaled = True
        self.resource.save()

        # Create February usage (previous month)
        plan_period, _ = marketplace_models.ResourcePlanPeriod.objects.get_or_create(
            resource=self.resource,
            plan=self.resource.plan,
            start=datetime.date(2026, 2, 1),
        )
        marketplace_models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.component,
            usage=Decimal("1300"),
            date=datetime.date(2026, 2, 15),
            billing_period=datetime.date(2026, 2, 1),
            plan_period=plan_period,
        )

        # Simulate period boundary reset task
        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        self.resource.refresh_from_db()

        self.assertFalse(
            self.resource.paused,
            "Resource should be un-paused at the start of a new monthly period",
        )
        self.assertFalse(
            self.resource.downscaled,
            "Resource should be un-downscaled at the start of a new monthly period",
        )


class ResetSlurmPoliciesOnPeriodBoundaryTaskTest(TestCase):
    """Integration tests for the reset_slurm_policies_on_period_boundary task itself."""

    def setUp(self):
        _disconnect_billing_signals()

        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = "Marketplace.Slurm"
        self.offering.save()

        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            name="Node hours",
            billing_type=marketplace_models.BillingTypes.USAGE,
        )

        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            backend_id="test-account",
            limits={"node": 1000},
        )

        marketplace_models.PlanComponent.objects.get_or_create(
            plan=self.resource.plan,
            component=self.component,
            defaults={"amount": 1000, "price": 1},
        )

        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.15,
            carryover_enabled=False,
            raw_usage_reset=True,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )

        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )

    def tearDown(self):
        _reconnect_billing_signals()

    @freeze_time("2026-03-01")
    def test_task_queues_evaluation_for_paused_resources(self):
        """Task queues evaluation for paused resources with usage below threshold."""
        self.resource.paused = True
        self.resource.save()

        with patch.object(
            tasks.evaluate_resource_against_policy, "delay"
        ) as mock_delay:
            result = tasks.reset_slurm_policies_on_period_boundary()

        mock_delay.assert_called_once_with(
            str(self.resource.uuid), str(self.policy.uuid)
        )
        self.assertEqual(result["evaluated"], 1)

    @freeze_time("2026-03-01")
    def test_task_skips_non_paused_resources(self):
        """Task does not queue evaluation for resources that are not paused/downscaled."""
        # Resource is neither paused nor downscaled
        self.resource.paused = False
        self.resource.downscaled = False
        self.resource.save()

        with patch.object(
            tasks.evaluate_resource_against_policy, "delay"
        ) as mock_delay:
            result = tasks.reset_slurm_policies_on_period_boundary()

        mock_delay.assert_not_called()
        self.assertEqual(result["evaluated"], 0)

    @freeze_time("2026-02-15")
    def test_task_skips_resources_with_high_usage_in_current_period(self):
        """Task does not queue evaluation when current period usage >= 100%."""
        self.resource.paused = True
        self.resource.save()

        # Create usage in current period (February) that exceeds limit
        plan_period, _ = marketplace_models.ResourcePlanPeriod.objects.get_or_create(
            resource=self.resource,
            plan=self.resource.plan,
            start=datetime.date(2026, 2, 1),
        )
        marketplace_models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.component,
            usage=Decimal("1300"),
            date=datetime.date(2026, 2, 15),
            billing_period=datetime.date(2026, 2, 1),
            plan_period=plan_period,
        )

        with patch.object(
            tasks.evaluate_resource_against_policy, "delay"
        ) as mock_delay:
            result = tasks.reset_slurm_policies_on_period_boundary()

        mock_delay.assert_not_called()
        self.assertEqual(result["evaluated"], 0)

    def test_task_skips_total_period_policies(self):
        """Task skips policies with period=TOTAL (no boundary to reset)."""
        self.policy.period = invoices_models.PeriodMixin.Periods.TOTAL
        self.policy.save()
        self.resource.paused = True
        self.resource.save()

        with patch.object(
            tasks.evaluate_resource_against_policy, "delay"
        ) as mock_delay:
            result = tasks.reset_slurm_policies_on_period_boundary()

        mock_delay.assert_not_called()
        self.assertEqual(result["evaluated"], 0)


class ForcePeriodResetAPITest(test.APITestCase):
    """API-level tests for the force-period-reset endpoint."""

    def setUp(self):
        _disconnect_billing_signals()

        self.customer = structure_factories.CustomerFactory()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.offering.type = "Marketplace.Slurm"
        self.offering.save()

        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            name="Node hours",
            billing_type=marketplace_models.BillingTypes.USAGE,
        )

        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            backend_id="test-account",
            limits={"node": 1000},
            paused=True,
            downscaled=True,
        )

        marketplace_models.PlanComponent.objects.get_or_create(
            plan=self.resource.plan,
            component=self.component,
            defaults={"amount": 1000, "price": 1},
        )

        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.15,
            carryover_enabled=False,
            raw_usage_reset=True,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )

        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )

        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.regular_user = structure_factories.UserFactory()
        self.url = SlurmPeriodicUsagePolicyFactory.get_url(
            self.policy, action="force-period-reset"
        )

    def tearDown(self):
        _reconnect_billing_signals()

    @freeze_time("2026-03-01")
    def test_staff_can_call_force_period_reset(self):
        """Staff user gets 200 and paused resource is reset."""
        self.client.force_authenticate(self.staff_user)
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("policy_uuid", response.data)
        self.assertIn("billing_period", response.data)
        self.assertIn("resources", response.data)
        self.assertEqual(len(response.data["resources"]), 1)

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)
        self.assertFalse(self.resource.downscaled)

    def test_non_staff_gets_403(self):
        """Non-staff user is forbidden from calling force_period_reset."""
        self.client.force_authenticate(self.regular_user)
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_gets_401(self):
        """Unauthenticated request returns 401."""
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @freeze_time("2026-03-01")
    def test_filter_by_resource_uuid(self):
        """With resource_uuid, only that resource is evaluated."""
        # Create a second paused resource
        resource2 = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            backend_id="test-account-2",
            limits={"node": 1000},
            paused=True,
        )

        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            self.url, {"resource_uuid": str(self.resource.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Only the targeted resource should appear in results
        result_uuids = [str(r["resource_uuid"]) for r in response.data["resources"]]
        self.assertIn(str(self.resource.uuid), result_uuids)
        self.assertNotIn(str(resource2.uuid), result_uuids)

    @freeze_time("2026-03-01")
    def test_non_paused_resources_excluded(self):
        """Resources that are not paused/downscaled are not in the results."""
        self.resource.paused = False
        self.resource.downscaled = False
        self.resource.save()

        self.client.force_authenticate(self.staff_user)
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["resources"]), 0)


class CarryoverAndGraceTest(TestCase):
    """Verify that carryover and grace_ratio actually work end-to-end.

    Carryover formula (per component):
        unused = max(0, base - prev_usage)
        cap = carryover_factor% * base
        carryover = min(unused, cap)
        total_allocation = base + carryover

    Grace threshold:
        pause at (1 + grace_ratio) * 100 % of total_allocation
        downscale at 100% of total_allocation
    """

    def setUp(self):
        _disconnect_billing_signals()

        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = "Marketplace.Slurm"
        self.offering.save()

        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="node",
            name="Node hours",
            billing_type=marketplace_models.BillingTypes.USAGE,
        )

        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            backend_id="test-account",
            limits={"node": 1000},
        )

        marketplace_models.PlanComponent.objects.get_or_create(
            plan=self.resource.plan,
            component=self.component,
            defaults={"amount": 1000, "price": 1},
        )

    def tearDown(self):
        _reconnect_billing_signals()

    def _create_usage(self, usage_amount, billing_date):
        plan_period, _ = marketplace_models.ResourcePlanPeriod.objects.get_or_create(
            resource=self.resource,
            plan=self.resource.plan,
            start=billing_date,
        )
        return marketplace_models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.component,
            usage=Decimal(str(usage_amount)),
            date=billing_date,
            billing_period=billing_date,
            plan_period=plan_period,
        )

    # -- Carryover tests --

    @freeze_time("2026-04-15")
    def test_carryover_increases_allocation(self):
        """Unused allocation from previous month carries over, increasing effective allocation.

        Setup: base=1000, prev_usage=400, carryover_factor=50%
        Expected: unused=600, cap=500, carryover=500, total=1500
        Usage in current month: 1200 → 1200/1500 = 80%
        Without carryover: 1200/1000 = 120%
        """
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=True,
            carryover_factor=50,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )
        models.OfferingComponentLimit.objects.create(
            policy=policy,
            component=self.component,
            limit=1000,
        )

        # March usage: 400 (previous month)
        self._create_usage(400, datetime.date(2026, 3, 1))
        # April usage: 1200 (current month)
        self._create_usage(1200, datetime.date(2026, 4, 1))

        usage_pct = policy.get_resource_usage_percentage(self.resource)

        # With carryover: total_alloc = 1000 + min(600, 500) = 1500
        # usage_pct = 1200/1500 * 100 = 80%
        self.assertAlmostEqual(usage_pct, 80.0, places=0)

    @freeze_time("2026-04-15")
    def test_carryover_disabled_uses_base_allocation_only(self):
        """With carryover disabled, only base allocation is used.

        Same scenario but carryover_enabled=False.
        Usage in current month: 1200 → 1200/1000 = 120%
        """
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )
        models.OfferingComponentLimit.objects.create(
            policy=policy,
            component=self.component,
            limit=1000,
        )

        # March usage: 400 (irrelevant when carryover disabled)
        self._create_usage(400, datetime.date(2026, 3, 1))
        # April usage: 1200
        self._create_usage(1200, datetime.date(2026, 4, 1))

        usage_pct = policy.get_resource_usage_percentage(self.resource)

        # No carryover: 1200/1000 * 100 = 120%
        self.assertAlmostEqual(usage_pct, 120.0, places=0)

    @freeze_time("2026-04-15")
    def test_carryover_capped_at_factor(self):
        """Carryover is capped at carryover_factor% of base, even if more unused.

        Setup: base=1000, prev_usage=0, carryover_factor=30%
        Expected: unused=1000, cap=300, carryover=300, total=1300
        """
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=True,
            carryover_factor=30,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )
        models.OfferingComponentLimit.objects.create(
            policy=policy,
            component=self.component,
            limit=1000,
        )

        # March: no usage (fully unused)
        # April usage: 1300
        self._create_usage(1300, datetime.date(2026, 4, 1))

        usage_pct = policy.get_resource_usage_percentage(self.resource)

        # Carryover = min(1000, 300) = 300, total = 1300
        # 1300/1300 * 100 = 100%
        self.assertAlmostEqual(usage_pct, 100.0, places=0)

    @freeze_time("2026-04-15")
    def test_carryover_zero_when_previous_fully_used(self):
        """No carryover when previous period was fully used.

        Setup: base=1000, prev_usage=1200, carryover_factor=50%
        Expected: unused=0, carryover=0, total=1000
        """
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=True,
            carryover_factor=50,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )
        models.OfferingComponentLimit.objects.create(
            policy=policy,
            component=self.component,
            limit=1000,
        )

        # March: fully used (even overused)
        self._create_usage(1200, datetime.date(2026, 3, 1))
        # April usage: 800
        self._create_usage(800, datetime.date(2026, 4, 1))

        usage_pct = policy.get_resource_usage_percentage(self.resource)

        # unused = max(0, 1000-1200) = 0, carryover = 0, total = 1000
        # 800/1000 * 100 = 80%
        self.assertAlmostEqual(usage_pct, 80.0, places=0)

    # -- Grace ratio tests --

    @freeze_time("2026-03-15")
    def test_grace_ratio_prevents_pause_below_grace_limit(self):
        """Resource at 110% usage is downscaled but NOT paused with grace_ratio=0.15.

        Thresholds: downscale at 100%, pause at 115%.
        """
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.15,
            carryover_enabled=False,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )
        models.OfferingComponentLimit.objects.create(
            policy=policy,
            component=self.component,
            limit=1000,
        )

        # 110% usage — above downscale (100%) but below grace (115%)
        self._create_usage(1100, datetime.date(2026, 3, 1))

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(policy.uuid)
        )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled, "Should be downscaled at 110%")
        self.assertFalse(
            self.resource.paused, "Should NOT be paused at 110% (grace=115%)"
        )

    @freeze_time("2026-03-15")
    def test_grace_ratio_triggers_pause_above_grace_limit(self):
        """Resource at 120% usage is both downscaled AND paused with grace_ratio=0.15.

        Thresholds: downscale at 100%, pause at 115%.
        """
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.15,
            carryover_enabled=False,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )
        models.OfferingComponentLimit.objects.create(
            policy=policy,
            component=self.component,
            limit=1000,
        )

        # 120% usage — above grace limit (115%)
        self._create_usage(1200, datetime.date(2026, 3, 1))

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(policy.uuid)
        )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled, "Should be downscaled at 120%")
        self.assertTrue(self.resource.paused, "Should be paused at 120% (grace=115%)")

    # -- Combined carryover + grace test --

    @freeze_time("2026-04-15")
    def test_carryover_expands_allocation_so_grace_not_hit(self):
        """Carryover expands allocation enough that grace threshold is not reached.

        Without carryover: base=1000, usage=1300, pct=130%, grace=120% → PAUSED
        With carryover: base=1000, prev_usage=200, factor=50%
            unused=800, cap=500, carry=500, total=1500
            usage=1300, pct=86.7%, grace=120% → NOT paused, NOT downscaled
        """
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=True,
            carryover_factor=50,
            period=invoices_models.PeriodMixin.Periods.MONTH_1,
        )
        models.OfferingComponentLimit.objects.create(
            policy=policy,
            component=self.component,
            limit=1000,
        )

        # March: low usage → high carryover
        self._create_usage(200, datetime.date(2026, 3, 1))
        # April: 1300 usage
        self._create_usage(1300, datetime.date(2026, 4, 1))

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(policy.uuid)
        )

        self.resource.refresh_from_db()
        # With carryover: 1300/1500 = 86.7% — below 100% downscale threshold
        self.assertFalse(
            self.resource.downscaled,
            "Carryover should expand allocation so 1300 usage is below 100%",
        )
        self.assertFalse(
            self.resource.paused,
            "Carryover should expand allocation so 1300 usage is below grace limit",
        )
