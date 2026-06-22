"""Tests for Celery-based SLURM policy evaluation."""

import datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from waldur_core.logging.tests.factories import EventSubscriptionQueueFactory
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.policy import models, tasks
from waldur_mastermind.policy.serializers import SlurmPeriodicUsagePolicySerializer
from waldur_mastermind.policy.tests import factories


class TestSlurmCeleryPolicyEvaluation(TestCase):
    """Test Celery-based SLURM policy evaluation tasks."""

    def _create_plan_period(self, resource):
        """Helper to create a ResourcePlanPeriod for a resource."""
        return marketplace_models.ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
        )

    def _create_component_usage(
        self, resource, component, usage_amount, plan_period=None
    ):
        """Helper to create ComponentUsage with proper plan_period."""
        if plan_period is None:
            plan_period = self._create_plan_period(resource)
        return marketplace_models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            usage=Decimal(str(usage_amount)),
            date=timezone.now(),
            billing_period=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
            plan_period=plan_period,
        )

    def setUp(self):
        """Set up test data for policy evaluation tests."""
        self.offering = factories.OfferingFactory(type="Marketplace.Slurm")
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)

        # Create component for node-hours
        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="node-hours", name="Node hours"
        )

        # Create test resources with per-component limits
        self.resource_low_usage = factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            name="low-usage-resource",
            backend_id="slurm-account-low",
            limits={"node-hours": 1000},
        )

        self.resource_high_usage = factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            name="high-usage-resource",
            backend_id="slurm-account-high",
            limits={"node-hours": 1000},
        )

        # Create SLURM periodic usage policy
        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners,request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,  # Disable for simpler test calculations
            carryover_factor=15,
            limit_type="GrpTRESMins",
            tres_billing_enabled=True,
            period=3,
        )

        # Create component limit
        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,  # 1000 node-hours quarterly
        )

        # Ensure plan has proper components with allocations
        if not self.resource_low_usage.plan.components.filter(
            component=self.component
        ).exists():
            marketplace_models.PlanComponent.objects.create(
                plan=self.resource_low_usage.plan,
                component=self.component,
                amount=1000,  # Base allocation
                price=1,
            )
        if not self.resource_high_usage.plan.components.filter(
            component=self.component
        ).exists():
            marketplace_models.PlanComponent.objects.create(
                plan=self.resource_high_usage.plan,
                component=self.component,
                amount=1000,  # Base allocation
                price=1,
            )

    def test_evaluate_slurm_resource_policy_task(self):
        """Test background task queues individual resource evaluations."""

        with patch(
            "waldur_mastermind.policy.tasks.evaluate_resource_against_policy.delay"
        ) as mock_delay:
            # Execute the task
            tasks.evaluate_slurm_resource_policy(str(self.resource_low_usage.uuid))

            # Verify individual evaluation was queued
            mock_delay.assert_called_once_with(
                str(self.resource_low_usage.uuid), str(self.policy.uuid)
            )

    def test_evaluate_resource_against_policy_below_threshold(self):
        """Test resource evaluation when usage is below thresholds."""

        # Create low usage (50% of allocation)
        self._create_component_usage(self.resource_low_usage, self.component, 500)

        # Execute policy evaluation
        tasks.evaluate_resource_against_policy(
            str(self.resource_low_usage.uuid), str(self.policy.uuid)
        )

        # Refresh resource state
        self.resource_low_usage.refresh_from_db()

        # Verify no restrictions applied
        self.assertFalse(self.resource_low_usage.downscaled)
        self.assertFalse(self.resource_low_usage.paused)

    def test_evaluate_resource_against_policy_above_threshold(self):
        """Test resource evaluation when usage exceeds thresholds."""

        # Create high usage (150% of allocation)
        self._create_component_usage(self.resource_high_usage, self.component, 1500)

        # Execute policy evaluation
        with self.assertLogs("waldur_mastermind.policy.tasks", level="INFO") as logs:
            tasks.evaluate_resource_against_policy(
                str(self.resource_high_usage.uuid), str(self.policy.uuid)
            )

        # Refresh resource state
        self.resource_high_usage.refresh_from_db()

        # Print logs for debugging
        print("Policy evaluation logs:")
        for log in logs.output:
            print(f"  {log}")

        # Debug the usage calculation
        current_period = self.policy._get_current_period()
        base_allocation = self.policy._get_base_allocation(self.resource_high_usage)
        current_usage = self.policy._get_current_period_usage(
            self.resource_high_usage, current_period
        )
        print(f"Current period: {current_period}")
        print(f"Base allocation: {base_allocation}")
        print(f"Current usage: {current_usage}")

        # Check what component usage was created
        usage_objects = marketplace_models.ComponentUsage.objects.filter(
            resource=self.resource_high_usage, component=self.component
        )
        for usage in usage_objects:
            print(
                f"ComponentUsage: {usage.usage}, billing_period: {usage.billing_period}, plan_period: {usage.plan_period}"
            )

        # Verify restrictions applied (150% > 120% grace limit)
        self.assertTrue(self.resource_high_usage.downscaled)
        self.assertTrue(self.resource_high_usage.paused)

    def test_automatic_recovery_when_usage_decreases(self):
        """Test automatic resource recovery when usage drops below thresholds."""

        # Start with restricted resource
        self.resource_high_usage.downscaled = True
        self.resource_high_usage.paused = True
        self.resource_high_usage.save()

        # Create low usage (30% of allocation)
        self._create_component_usage(self.resource_high_usage, self.component, 300)

        # Execute policy evaluation
        tasks.evaluate_resource_against_policy(
            str(self.resource_high_usage.uuid), str(self.policy.uuid)
        )

        # Refresh resource state
        self.resource_high_usage.refresh_from_db()

        # Verify automatic recovery (restrictions removed)
        self.assertFalse(self.resource_high_usage.downscaled)
        self.assertFalse(self.resource_high_usage.paused)

    def test_resource_independence(self):
        """Test that resources are evaluated independently."""

        # Resource 1: Low usage (30%)
        self._create_component_usage(self.resource_low_usage, self.component, 300)

        # Resource 2: High usage (150%)
        self._create_component_usage(self.resource_high_usage, self.component, 1500)

        # Evaluate both resources
        tasks.evaluate_resource_against_policy(
            str(self.resource_low_usage.uuid), str(self.policy.uuid)
        )
        tasks.evaluate_resource_against_policy(
            str(self.resource_high_usage.uuid), str(self.policy.uuid)
        )

        # Refresh states
        self.resource_low_usage.refresh_from_db()
        self.resource_high_usage.refresh_from_db()

        # Verify independence
        self.assertFalse(
            self.resource_low_usage.downscaled
        )  # Not affected by other resource
        self.assertFalse(self.resource_low_usage.paused)

        self.assertTrue(self.resource_high_usage.downscaled)  # Affected by own usage
        self.assertTrue(self.resource_high_usage.paused)

    def test_check_other_resources_triggered(self):
        """Test checking if other resources still trigger policy."""

        # Create usage for multiple resources
        self._create_component_usage(
            self.resource_low_usage, self.component, 300
        )  # 30% - below thresholds
        self._create_component_usage(
            self.resource_high_usage, self.component, 1200
        )  # 120% - above thresholds

        # Check if other resources trigger when excluding low usage resource
        result = tasks.check_other_resources_triggered.apply(
            args=[str(self.policy.uuid), str(self.resource_low_usage.uuid)]
        ).get()

        # Should return True because high usage resource still triggers
        self.assertTrue(result)

        # Check when excluding high usage resource
        result = tasks.check_other_resources_triggered.apply(
            args=[str(self.policy.uuid), str(self.resource_high_usage.uuid)]
        ).get()

        # Should return False because only low usage resource remains
        self.assertFalse(result)

    def test_policy_state_management(self):
        """Test policy has_fired state management with resource-specific evaluation."""

        # Initially policy should not be fired
        self.assertFalse(self.policy.has_fired)

        # Create high usage that should trigger policy
        self._create_component_usage(self.resource_high_usage, self.component, 1500)

        # Execute evaluation
        tasks.evaluate_resource_against_policy(
            str(self.resource_high_usage.uuid), str(self.policy.uuid)
        )

        # Policy should be fired
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.has_fired)
        self.assertIsNotNone(self.policy.fired_datetime)

    @patch("waldur_mastermind.policy.tasks.notify_about_resource_usage.delay")
    def test_notification_queuing(self, mock_notify):
        """Test that notifications are properly queued for background processing."""

        # Create usage that triggers notification (85%)
        self._create_component_usage(self.resource_low_usage, self.component, 850)

        # Execute evaluation
        tasks.evaluate_resource_against_policy(
            str(self.resource_low_usage.uuid), str(self.policy.uuid)
        )

        # Verify notification was queued
        mock_notify.assert_called_once_with(
            str(self.resource_low_usage.uuid),
            str(self.policy.uuid),
            85.0,  # usage percentage
        )

    @patch("waldur_mastermind.policy.tasks.notify_about_resource_usage.delay")
    def test_notification_sent_only_once_per_billing_period(self, mock_notify):
        """Test that notification is sent only once per resource per billing period."""

        # Create usage that triggers notification (85%)
        self._create_component_usage(self.resource_low_usage, self.component, 850)

        # First evaluation — should send notification
        tasks.evaluate_resource_against_policy(
            str(self.resource_low_usage.uuid), str(self.policy.uuid)
        )
        mock_notify.assert_called_once()
        mock_notify.reset_mock()

        # Second evaluation — should NOT send notification again
        tasks.evaluate_resource_against_policy(
            str(self.resource_low_usage.uuid), str(self.policy.uuid)
        )
        mock_notify.assert_not_called()

    def test_quarterly_period_evaluation(self):
        """Test quarterly period evaluation logic."""

        # Create usage in different billing periods
        # Previous quarter (Q3 2025 - use July as an example)
        previous_quarter_date = datetime.date(2025, 7, 1)  # Q3 2025
        previous_plan_period = marketplace_models.ResourcePlanPeriod.objects.create(
            resource=self.resource_low_usage,
            plan=self.resource_low_usage.plan,
            start=timezone.datetime.combine(
                previous_quarter_date,
                timezone.datetime.min.time(),
                datetime.UTC,
            ),
        )
        marketplace_models.ComponentUsage.objects.create(
            resource=self.resource_low_usage,
            component=self.component,
            usage=Decimal("400"),
            date=previous_quarter_date,
            billing_period=previous_quarter_date,
            plan_period=previous_plan_period,
        )

        # Current quarter (Q4 2025 - use December as an example)
        current_quarter_date = datetime.date(2025, 12, 1)  # Q4 2025
        current_plan_period = marketplace_models.ResourcePlanPeriod.objects.create(
            resource=self.resource_low_usage,
            plan=self.resource_low_usage.plan,
            start=timezone.datetime.combine(
                current_quarter_date,
                timezone.datetime.min.time(),
                datetime.UTC,
            ),
        )
        marketplace_models.ComponentUsage.objects.create(
            resource=self.resource_low_usage,
            component=self.component,
            usage=Decimal("600"),
            date=current_quarter_date,
            billing_period=current_quarter_date,
            plan_period=current_plan_period,
        )

        # Policy should evaluate current quarter only (600Nh = 60%)
        tasks.evaluate_resource_against_policy(
            str(self.resource_low_usage.uuid), str(self.policy.uuid)
        )

        # Refresh resource state
        self.resource_low_usage.refresh_from_db()

        # Should not be restricted (60% < 80% notification threshold)
        self.assertFalse(self.resource_low_usage.downscaled)
        self.assertFalse(self.resource_low_usage.paused)

    def test_grace_period_logic(self):
        """Test grace period calculation and enforcement."""

        # Test usage at exactly 100% (should trigger downscaling)
        self._create_component_usage(
            self.resource_low_usage, self.component, 1000
        )  # Exactly 100%

        tasks.evaluate_resource_against_policy(
            str(self.resource_low_usage.uuid), str(self.policy.uuid)
        )

        self.resource_low_usage.refresh_from_db()

        # Should be downscaled but not paused (100% < 120% grace limit)
        self.assertTrue(self.resource_low_usage.downscaled)
        self.assertFalse(self.resource_low_usage.paused)

        # Test usage at grace limit (120% - should trigger pausing)
        marketplace_models.ComponentUsage.objects.filter(
            resource=self.resource_low_usage
        ).update(usage=Decimal("1200"))  # 120% - grace limit

        tasks.evaluate_resource_against_policy(
            str(self.resource_low_usage.uuid), str(self.policy.uuid)
        )

        self.resource_low_usage.refresh_from_db()

        # Should be both downscaled and paused
        self.assertTrue(self.resource_low_usage.downscaled)
        self.assertTrue(self.resource_low_usage.paused)

    def test_stale_evaluation_does_not_clobber_newer_unpause(self):
        """Regression: a stale high-usage evaluation must not re-pause a resource
        after a newer low-usage evaluation has cleared it.

        Reproduces the lost-update race: evaluation A is triggered by a high
        ComponentUsage write and decides {pause, downscale}; before A commits, a
        newer evaluation B (zero usage) un-pauses and un-downscales the resource.
        The fix locks the resource row and recomputes usage *under the lock*, so
        A's decision reflects the state B committed (usage 0%) rather than the
        stale 150% snapshot it started with — A becomes a no-op instead of
        clobbering the restore.

        We model B's concurrent commit by dropping the usage to 0 at the moment
        A acquires the row lock (select_for_update). Without the fix, usage is
        read once before any lock and the resource stays stuck at (True, True).
        """
        # Resource is currently restricted (an earlier high-usage eval paused it).
        self.resource_high_usage.paused = True
        self.resource_high_usage.downscaled = True
        self.resource_high_usage.save()

        # High usage is present at task start — the trigger for this (now stale)
        # evaluation A.
        usage = self._create_component_usage(
            self.resource_high_usage, self.component, 1500
        )

        real_select_for_update = marketplace_models.Resource.objects.select_for_update

        def drop_usage_then_lock(*args, **kwargs):
            # Simulate concurrent evaluation B committing usage=0 just before
            # this evaluation acquires the row lock.
            marketplace_models.ComponentUsage.objects.filter(pk=usage.pk).update(
                usage=Decimal("0")
            )
            return real_select_for_update(*args, **kwargs)

        with patch.object(
            marketplace_models.Resource.objects,
            "select_for_update",
            side_effect=drop_usage_then_lock,
        ):
            tasks.evaluate_resource_against_policy(
                str(self.resource_high_usage.uuid), str(self.policy.uuid)
            )

        self.resource_high_usage.refresh_from_db()

        # Usage is 0% under the lock → the restore must stand, not be clobbered.
        self.assertFalse(self.resource_high_usage.paused)
        self.assertFalse(self.resource_high_usage.downscaled)


class TestSlurmPolicySignalHandlers(TestCase):
    """Test signal handlers for SLURM policy evaluation."""

    def _create_plan_period(self, resource):
        """Helper to create a ResourcePlanPeriod for a resource."""
        return marketplace_models.ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
        )

    def _create_component_usage(
        self, resource, component, usage_amount, plan_period=None
    ):
        """Helper to create ComponentUsage with proper plan_period."""
        if plan_period is None:
            plan_period = self._create_plan_period(resource)
        return marketplace_models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            usage=Decimal(str(usage_amount)),
            date=timezone.now(),
            billing_period=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
            plan_period=plan_period,
        )

    def setUp(self):
        """Set up test data for signal handler tests."""
        self.offering = factories.OfferingFactory(type="Marketplace.Slurm")
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            limits={"node-hours": 1000},
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="node-hours"
        )

        # Create policy
        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners,request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            period=3,
        )

        # Create component limit
        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )

        # Create plan component for proper allocation
        marketplace_models.PlanComponent.objects.create(
            plan=self.resource.plan,
            component=self.component,
            amount=1000,
            price=1,
        )

    @patch("waldur_mastermind.policy.tasks.evaluate_slurm_resource_policy.delay")
    def test_signal_handler_queues_background_task(self, mock_delay):
        """Test that signal handler queues background evaluation task."""

        # Create ComponentUsage (this automatically triggers the signal)
        self._create_component_usage(self.resource, self.component, 800)

        # Verify background task was queued for the resource
        mock_delay.assert_called_once_with(str(self.resource.uuid))

    def test_signal_handler_no_policy_no_task(self):
        """Test signal handler doesn't queue tasks when no SLURM policies exist."""

        # Delete the policy
        self.policy.delete()

        with patch(
            "waldur_mastermind.policy.tasks.evaluate_slurm_resource_policy.delay"
        ) as mock_delay:
            from waldur_mastermind.policy.handlers import (
                slurm_periodic_usage_policy_trigger_handler,
            )

            component_usage = self._create_component_usage(
                self.resource, self.component, 800
            )

            slurm_periodic_usage_policy_trigger_handler(
                sender=marketplace_models.ComponentUsage,
                instance=component_usage,
                created=True,
            )

            # Should not queue task when no policies exist
            mock_delay.assert_not_called()


class TestSlurmPolicyRecovery(TestCase):
    """Test automatic policy recovery mechanisms."""

    def _create_plan_period(self, resource):
        """Helper to create a ResourcePlanPeriod for a resource."""
        return marketplace_models.ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
        )

    def _create_component_usage(
        self, resource, component, usage_amount, plan_period=None
    ):
        """Helper to create ComponentUsage with proper plan_period."""
        if plan_period is None:
            plan_period = self._create_plan_period(resource)
        return marketplace_models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            usage=Decimal(str(usage_amount)),
            date=timezone.now(),
            billing_period=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
            plan_period=plan_period,
        )

    def setUp(self):
        """Set up test data for recovery tests."""
        self.offering = factories.OfferingFactory(type="Marketplace.Slurm")
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            downscaled=True,  # Start with restrictions
            paused=True,
            limits={"node-hours": 1000},
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="node-hours"
        )

        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,  # Disable for simpler test calculations
            has_fired=True,  # Start with fired policy
        )

        models.OfferingComponentLimit.objects.create(
            policy=self.policy, component=self.component, limit=1000
        )

        # Create plan component for proper allocation
        marketplace_models.PlanComponent.objects.create(
            plan=self.resource.plan,
            component=self.component,
            amount=1000,
            price=1,
        )

    def test_downscaling_recovery(self):
        """Test automatic removal of downscaling when usage drops below 100%."""

        # Create low usage (50% - below downscaling threshold)
        self._create_component_usage(self.resource, self.component, 500)

        # Execute evaluation
        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        # Refresh resource
        self.resource.refresh_from_db()

        # Downscaling should be removed (50% < 100%)
        self.assertFalse(self.resource.downscaled)
        # Pausing should also be removed (50% < 120%)
        self.assertFalse(self.resource.paused)

    def test_partial_recovery(self):
        """Test partial recovery - remove pausing but keep downscaling."""

        # Create usage at 110% (above downscaling, below grace limit)
        self._create_component_usage(self.resource, self.component, 1100)

        # Execute evaluation
        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        # Refresh resource
        self.resource.refresh_from_db()

        # Should be downscaled (110% >= 100%) but not paused (110% < 120%)
        self.assertTrue(self.resource.downscaled)
        self.assertFalse(self.resource.paused)

    @patch(
        "waldur_mastermind.policy.tasks.check_other_resources_triggered.apply",
        side_effect=RuntimeError(
            "Never call result.get() within a task! "
            "See https://docs.celeryq.dev/en/latest/userguide/tasks.html"
            "#avoid-launching-synchronous-subtasks"
        ),
    )
    def test_policy_reset_does_not_call_synchronous_subtask(self, mock_apply):
        """Test that policy reset check calls function directly, not via .apply().get().

        Regression test for CSCS-220: calling result.get() within a Celery task
        raises RuntimeError('Never call result.get() within a task!').
        The check_other_resources_triggered logic must be called as a plain
        function, not dispatched through Celery.
        """
        # Low usage — no actions needed, but policy.has_fired is True
        self._create_component_usage(self.resource, self.component, 500)

        # This must NOT call check_other_resources_triggered.apply().get()
        # which would raise RuntimeError in a real Celery worker
        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        # The synchronous .apply() must NOT have been called
        mock_apply.assert_not_called()

        # Policy should be reset since no resources exceed thresholds
        self.policy.refresh_from_db()
        self.assertFalse(self.policy.has_fired)

        # Resource restrictions should be removed
        self.resource.refresh_from_db()
        self.assertFalse(self.resource.downscaled)
        self.assertFalse(self.resource.paused)


class TestSlurmPolicyPerformance(TestCase):
    """Test performance characteristics of Celery-based policy evaluation."""

    def _create_plan_period(self, resource):
        """Helper to create a ResourcePlanPeriod for a resource."""
        return marketplace_models.ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
        )

    def _create_component_usage(
        self, resource, component, usage_amount, plan_period=None
    ):
        """Helper to create ComponentUsage with proper plan_period."""
        if plan_period is None:
            plan_period = self._create_plan_period(resource)
        return marketplace_models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            usage=Decimal(str(usage_amount)),
            date=timezone.now(),
            billing_period=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
            plan_period=plan_period,
        )

    def setUp(self):
        """Set up performance test data."""
        self.offering = factories.OfferingFactory(type="Marketplace.Slurm")
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="node-hours"
        )

        # Create multiple resources for performance testing
        self.resources = [
            factories.ResourceFactory(
                offering=self.offering,
                project=self.project,
                name=f"resource-{i}",
                limits={"node-hours": 1000},
            )
            for i in range(10)
        ]

        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,  # Disable for simpler test calculations
            period=3,
        )

        # Create component limit
        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )

        # Create plan components for all resources
        for resource in self.resources:
            if not resource.plan.components.filter(component=self.component).exists():
                marketplace_models.PlanComponent.objects.create(
                    plan=resource.plan,
                    component=self.component,
                    amount=1000,
                    price=1,
                )

    @patch("waldur_mastermind.policy.tasks.evaluate_resource_against_policy.delay")
    def test_parallel_resource_evaluation(self, mock_delay):
        """Test that multiple resources are evaluated in parallel."""

        # Queue evaluation for multiple resources
        for resource in self.resources[:5]:
            tasks.evaluate_slurm_resource_policy(str(resource.uuid))

        # Verify each resource gets individual evaluation task
        self.assertEqual(mock_delay.call_count, 5)

        # Verify each call has correct parameters
        for call in mock_delay.call_args_list:
            args = call[0]
            self.assertIn(args[0], [str(r.uuid) for r in self.resources])
            self.assertEqual(args[1], str(self.policy.uuid))

    def test_task_error_handling(self):
        """Test error handling in background tasks."""

        # Test with non-existent resource UUID
        with self.assertLogs("waldur_mastermind.policy.tasks", level="ERROR") as logs:
            tasks.evaluate_resource_against_policy(
                "non-existent-uuid", str(self.policy.uuid)
            )

        # Should log error without crashing
        self.assertIn("Policy evaluation failed - missing object", logs.output[0])

    def test_concurrent_resource_evaluation(self):
        """Test concurrent evaluation of multiple resources."""

        # Create different usage levels for resources
        usage_levels = [300, 800, 1100, 1500, 200]  # Mix of low/high usage

        for i, usage in enumerate(usage_levels):
            self._create_component_usage(self.resources[i], self.component, usage)

        # Execute evaluations concurrently (simulate Celery parallel execution)
        for i in range(len(usage_levels)):
            tasks.evaluate_resource_against_policy(
                str(self.resources[i].uuid), str(self.policy.uuid)
            )

        # Verify each resource state matches its individual usage
        expected_states = [
            (False, False),  # 300 (30%) - no restrictions
            (False, False),  # 800 (80%) - notification only, no restrictions
            (True, False),  # 1100 (110%) - downscaled, not paused
            (True, True),  # 1500 (150%) - downscaled and paused
            (False, False),  # 200 (20%) - no restrictions
        ]

        for i, (exp_downscaled, exp_paused) in enumerate(expected_states):
            self.resources[i].refresh_from_db()
            self.assertEqual(
                self.resources[i].downscaled,
                exp_downscaled,
                f"Resource {i} (usage {usage_levels[i]}) downscaled mismatch",
            )
            self.assertEqual(
                self.resources[i].paused,
                exp_paused,
                f"Resource {i} (usage {usage_levels[i]}) paused mismatch",
            )


class TestSlurmPolicyIntegration(TestCase):
    """Integration tests for complete SLURM policy workflow."""

    def _create_plan_period(self, resource):
        """Helper to create a ResourcePlanPeriod for a resource."""
        return marketplace_models.ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
        )

    def _create_component_usage(
        self, resource, component, usage_amount, plan_period=None
    ):
        """Helper to create ComponentUsage with proper plan_period."""
        if plan_period is None:
            plan_period = self._create_plan_period(resource)
        return marketplace_models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            usage=Decimal(str(usage_amount)),
            date=timezone.now(),
            billing_period=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
            plan_period=plan_period,
        )

    def setUp(self):
        """Set up integration test environment."""
        self.offering = factories.OfferingFactory(
            type="Marketplace.Slurm",
            plugin_options={"supports_downscaling": True, "supports_pausing": True},
        )
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            backend_id="slurm-test-account",
            limits={"node-hours": 1000},
        )
        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="node-hours"
        )

    @patch("waldur_mastermind.policy.tasks.evaluate_slurm_resource_policy.delay")
    def test_complete_workflow_signal_to_task(self, mock_delay):
        """Test complete workflow from ComponentUsage signal to task queuing."""

        # Create policy
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,  # Disable for simpler test calculations
            period=3,
        )

        models.OfferingComponentLimit.objects.create(
            policy=policy, component=self.component, limit=1000
        )

        # Create plan component
        marketplace_models.PlanComponent.objects.create(
            plan=self.resource.plan,
            component=self.component,
            amount=1000,
            price=1,
        )

        # Create ComponentUsage (this automatically triggers the signal)
        self._create_component_usage(self.resource, self.component, 1200)

        # Verify background task was queued for the resource
        mock_delay.assert_called_once_with(str(self.resource.uuid))

    def test_end_to_end_policy_application(self):
        """Test end-to-end policy application without mocks."""

        # Create policy
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,  # Disable for simpler test calculations
            period=3,
        )

        models.OfferingComponentLimit.objects.create(
            policy=policy, component=self.component, limit=1000
        )

        # Create plan component
        marketplace_models.PlanComponent.objects.create(
            plan=self.resource.plan,
            component=self.component,
            amount=1000,
            price=1,
        )

        # Create high usage ComponentUsage
        self._create_component_usage(self.resource, self.component, 1300)  # 130% usage

        # Execute background evaluation directly
        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(policy.uuid)
        )

        # Verify resource restrictions applied
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled)
        self.assertTrue(self.resource.paused)  # 130% > 120% grace limit

        # Test recovery - reduce usage
        marketplace_models.ComponentUsage.objects.filter(resource=self.resource).update(
            usage=Decimal("200")
        )  # 20% usage

        # Execute evaluation again
        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(policy.uuid)
        )

        # Verify automatic recovery
        self.resource.refresh_from_db()
        self.assertFalse(self.resource.downscaled)  # Recovered
        self.assertFalse(self.resource.paused)  # Recovered


class TestSlurmPolicyEvaluationSendsSTOMP(TestCase):
    """Test that policy evaluation sends STOMP messages to site agent for QoS changes.

    This verifies that when evaluate_resource_against_policy sets resource.downscaled
    or resource.paused, it also sends a STOMP message to the site agent so that the
    actual SLURM QoS is changed (e.g. from 'normal' to 'slowdown' or 'blocked').

    Without the STOMP message, the resource flags are set in the Waldur DB but
    no sacctmgr command is ever executed on SLURM, leaving QoS as 'normal'.
    """

    def _create_plan_period(self, resource):
        return marketplace_models.ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
        )

    def _create_component_usage(
        self, resource, component, usage_amount, plan_period=None
    ):
        if plan_period is None:
            plan_period = self._create_plan_period(resource)
        return marketplace_models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            usage=Decimal(str(usage_amount)),
            date=timezone.now(),
            billing_period=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
            plan_period=plan_period,
        )

    def setUp(self):
        self.offering = factories.OfferingFactory(
            type="Marketplace.Slurm",
            plugin_options={"supports_downscaling": True, "supports_pausing": True},
        )
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="node-hours", name="Node hours"
        )
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            name="test-resource",
            backend_id="slurm-test-account",
            limits={"node-hours": 1000},
        )
        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,
            period=3,
        )
        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )
        marketplace_models.PlanComponent.objects.create(
            plan=self.resource.plan,
            component=self.component,
            amount=1000,
            price=1,
        )

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_downscaling_triggers_stomp_message_to_site_agent(
        self, mock_apply_policy_actions
    ):
        """When usage >= 100% triggers downscaling, a STOMP message must be sent
        to the site agent so that SLURM QoS is changed from 'normal' to 'slowdown'.

        Without this, resource.downscaled is set in the DB but sacctmgr is never
        called and QoS remains 'normal' on the SLURM cluster.
        """
        mock_apply_policy_actions.return_value = True

        # Create usage at 110% - above downscaling threshold, below grace limit
        self._create_component_usage(self.resource, self.component, 1100)

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled)
        self.assertFalse(self.resource.paused)  # 110% < 120% grace limit

        # The critical assertion: apply_policy_actions must be called to send
        # the STOMP message with QoS settings to the site agent
        mock_apply_policy_actions.assert_called_once_with(self.resource)

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_pausing_triggers_stomp_message_to_site_agent(
        self, mock_apply_policy_actions
    ):
        """When usage >= grace limit triggers pausing, a STOMP message must be sent
        to the site agent so that SLURM QoS is changed to 'blocked'.
        """
        mock_apply_policy_actions.return_value = True

        # Create usage at 150% - above grace limit (120%)
        self._create_component_usage(self.resource, self.component, 1500)

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled)
        self.assertTrue(self.resource.paused)

        # STOMP message must be sent for the QoS change
        mock_apply_policy_actions.assert_called_once_with(self.resource)

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_recovery_triggers_stomp_message_to_reset_qos(
        self, mock_apply_policy_actions
    ):
        """When usage drops below thresholds, a STOMP message must be sent to
        reset SLURM QoS back to 'normal'.
        """
        mock_apply_policy_actions.return_value = True

        # Start with restricted resource
        self.resource.downscaled = True
        self.resource.paused = True
        self.resource.save()
        self.policy.has_fired = True
        self.policy.save()

        # Usage drops to 50% - below all thresholds
        self._create_component_usage(self.resource, self.component, 500)

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.downscaled)
        self.assertFalse(self.resource.paused)

        # STOMP message must be sent to reset QoS to 'normal' on SLURM
        mock_apply_policy_actions.assert_called_once_with(self.resource)

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_no_stomp_when_no_state_change(self, mock_apply_policy_actions):
        """When usage is below thresholds and resource is not restricted,
        no STOMP message should be sent (no state change).
        """
        mock_apply_policy_actions.return_value = True

        # Usage at 50% - no restrictions, no state change
        self._create_component_usage(self.resource, self.component, 500)

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.downscaled)
        self.assertFalse(self.resource.paused)

        # No state change -> no STOMP message needed
        mock_apply_policy_actions.assert_not_called()

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_no_stomp_when_already_downscaled_and_still_above_threshold(
        self, mock_apply_policy_actions
    ):
        """When resource is already downscaled and usage stays above threshold,
        no STOMP message should be sent (state unchanged).
        """
        mock_apply_policy_actions.return_value = True

        # Resource is already downscaled
        self.resource.downscaled = True
        self.resource.save()

        # Usage at 110% - still above threshold, state doesn't change
        self._create_component_usage(self.resource, self.component, 1100)

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled)
        self.assertFalse(self.resource.paused)

        # State unchanged (was downscaled, still downscaled) -> no STOMP
        mock_apply_policy_actions.assert_not_called()

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_escalation_from_downscaled_to_paused_triggers_stomp(
        self, mock_apply_policy_actions
    ):
        """When usage increases from slowdown range to blocked range,
        STOMP must be sent to escalate QoS from 'slowdown' to 'blocked'.
        """
        mock_apply_policy_actions.return_value = True

        # Resource is already downscaled (QoS = slowdown)
        self.resource.downscaled = True
        self.resource.save()

        # Usage increases to 150% - above grace limit (120%)
        self._create_component_usage(self.resource, self.component, 1500)

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled)
        self.assertTrue(self.resource.paused)  # Escalated to paused

        # State changed (paused added) -> STOMP must be sent
        mock_apply_policy_actions.assert_called_once_with(self.resource)

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_partial_recovery_from_paused_to_downscaled_triggers_stomp(
        self, mock_apply_policy_actions
    ):
        """When usage drops below grace limit but stays above 100%,
        STOMP must be sent to de-escalate QoS from 'blocked' to 'slowdown'.
        """
        mock_apply_policy_actions.return_value = True

        # Resource is both downscaled and paused (QoS = blocked)
        self.resource.downscaled = True
        self.resource.paused = True
        self.resource.save()
        self.policy.has_fired = True
        self.policy.save()

        # Usage drops to 110% - below grace limit, above downscaling threshold
        self._create_component_usage(self.resource, self.component, 1100)

        tasks.evaluate_resource_against_policy(
            str(self.resource.uuid), str(self.policy.uuid)
        )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.downscaled)  # Still above 100%
        self.assertFalse(self.resource.paused)  # Recovered from pausing

        # State changed (paused removed) -> STOMP must be sent
        mock_apply_policy_actions.assert_called_once_with(self.resource)


class TestSlurmPolicySTOMPPayloadContent(TestCase):
    """Test that STOMP message payload contains required fields for the site agent."""

    def _create_plan_period(self, resource):
        return marketplace_models.ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
        )

    def _create_component_usage(
        self, resource, component, usage_amount, plan_period=None
    ):
        if plan_period is None:
            plan_period = self._create_plan_period(resource)
        return marketplace_models.ComponentUsage.objects.create(
            resource=resource,
            component=component,
            usage=Decimal(str(usage_amount)),
            date=timezone.now(),
            billing_period=timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            ),
            plan_period=plan_period,
        )

    def setUp(self):
        self.offering = factories.OfferingFactory(
            type="Marketplace.Slurm",
            plugin_options={"supports_downscaling": True, "supports_pausing": True},
        )
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="node-hours", name="Node hours"
        )
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            name="test-resource",
            backend_id="slurm-test-account",
            limits={"node-hours": 1000},
        )
        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,
            period=3,
        )
        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )
        marketplace_models.PlanComponent.objects.create(
            plan=self.resource.plan,
            component=self.component,
            amount=1000,
            price=1,
        )

    def test_stomp_payload_contains_required_fields(self):
        """STOMP payload must include resource, offering, policy UUIDs, settings and action."""
        settings = self.policy.calculate_slurm_settings(self.resource)

        with (
            patch(
                "waldur_mastermind.marketplace.utils.prepare_messages", return_value=[]
            ) as mock_prepare,
            patch("waldur_core.logging.tasks.publish_messages.delay"),
        ):
            self.policy._send_settings_to_site_agent(self.resource, settings)

            mock_prepare.assert_called_once()
            payload = mock_prepare.call_args[1]["message_payload"]

            self.assertEqual(payload["resource_uuid"], str(self.resource.uuid))
            self.assertEqual(payload["backend_id"], self.resource.backend_id)
            self.assertEqual(payload["offering_uuid"], str(self.resource.offering.uuid))
            self.assertEqual(payload["policy_uuid"], str(self.policy.uuid))
            self.assertEqual(payload["action"], "apply_periodic_settings")
            self.assertIn("settings", payload)
            self.assertIn("timestamp", payload)

    def test_stomp_payload_does_not_contain_qos_fields(self):
        """STOMP payload must not include desired_qos, downscaled, or paused fields.

        The site agent determines QoS independently from SLURM usage data.
        """
        settings = self.policy.calculate_slurm_settings(self.resource)

        with (
            patch(
                "waldur_mastermind.marketplace.utils.prepare_messages", return_value=[]
            ) as mock_prepare,
            patch("waldur_core.logging.tasks.publish_messages.delay"),
        ):
            self.policy._send_settings_to_site_agent(self.resource, settings)

            mock_prepare.assert_called_once()
            payload = mock_prepare.call_args[1]["message_payload"]

            self.assertNotIn("desired_qos", payload)
            self.assertNotIn("downscaled", payload)
            self.assertNotIn("paused", payload)


class TestSlurmPolicySerializerWarnings(TestCase):
    """Test that serializer warns when no site agent queue is registered."""

    def setUp(self):
        self.offering = factories.OfferingFactory(type="Marketplace.Slurm")
        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="node-hours", name="Node hours"
        )
        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            period=3,
        )
        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )

    def test_warning_when_no_queue_registered(self):
        """Serializer output includes warnings when no EventSubscriptionQueue exists
        for the offering with object_type=resource_periodic_limits.
        """
        serializer = SlurmPeriodicUsagePolicySerializer(
            self.policy,
            context={"request": None, "view": type("View", (), {"kwargs": {}})()},
        )
        data = serializer.data
        self.assertIn("warnings", data)
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("No site agent has registered a queue", data["warnings"][0])

    def test_no_warning_when_queue_registered(self):
        """Serializer output has no warnings when an EventSubscriptionQueue exists
        for the offering with object_type=resource_periodic_limits.
        """
        EventSubscriptionQueueFactory(
            offering_uuid=self.offering.uuid,
            object_type="resource_periodic_limits",
        )
        serializer = SlurmPeriodicUsagePolicySerializer(
            self.policy,
            context={"request": None, "view": type("View", (), {"kwargs": {}})()},
        )
        data = serializer.data
        self.assertNotIn("warnings", data)


class TestGracePeriodPolicyInteraction(TestCase):
    """Test that policy evaluation respects project lifecycle grace period."""

    def setUp(self):
        self.offering = factories.OfferingFactory(
            type="Marketplace.Slurm",
            plugin_options={"supports_pausing": True},
        )
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(
            customer=self.customer,
            end_date=datetime.date(2020, 1, 1),
            grace_period_days=30,
        )

        self.component = factories.OfferingComponentFactory(
            offering=self.offering, type="node-hours", name="Node hours"
        )

        self.resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.project,
            name="test-resource",
            backend_id="slurm-account-test",
            limits={"node-hours": 1000},
        )

        self.policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners,request_slurm_resource_downscaling,request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,
            limit_type="GrpTRESMins",
            tres_billing_enabled=True,
            period=3,
        )

        models.OfferingComponentLimit.objects.create(
            policy=self.policy,
            component=self.component,
            limit=1000,
        )

        if not self.resource.plan.components.filter(component=self.component).exists():
            marketplace_models.PlanComponent.objects.create(
                plan=self.resource.plan,
                component=self.component,
                amount=1000,
                price=1,
            )

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_policy_pauses_resource_during_grace_period(
        self, mock_apply_policy_actions
    ):
        """Resource should be paused during grace period even with zero usage."""
        mock_apply_policy_actions.return_value = True

        # 2020-01-15 is within grace period (end_date=Jan 1, grace=30 days)
        with patch.object(
            type(self.project),
            "is_in_grace_period",
            new_callable=lambda: property(lambda self: True),
        ):
            tasks.evaluate_resource_against_policy(
                str(self.resource.uuid), str(self.policy.uuid)
            )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_policy_does_not_unpause_during_grace_period(
        self, mock_apply_policy_actions
    ):
        """Resource paused by grace period should stay paused even with low usage."""
        mock_apply_policy_actions.return_value = True
        self.resource.paused = True
        self.resource.save()

        with patch.object(
            type(self.project),
            "is_in_grace_period",
            new_callable=lambda: property(lambda self: True),
        ):
            tasks.evaluate_resource_against_policy(
                str(self.resource.uuid), str(self.policy.uuid)
            )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_policy_unpauses_when_grace_period_ends_and_usage_low(
        self, mock_apply_policy_actions
    ):
        """Resource should be unpaused when grace period ends and usage is below threshold."""
        mock_apply_policy_actions.return_value = True
        self.resource.paused = True
        self.resource.save()

        with patch.object(
            type(self.project),
            "is_in_grace_period",
            new_callable=lambda: property(lambda self: False),
        ):
            tasks.evaluate_resource_against_policy(
                str(self.resource.uuid), str(self.policy.uuid)
            )

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)

    @patch(
        "waldur_mastermind.policy.models.SlurmPeriodicUsagePolicy.apply_policy_actions"
    )
    def test_offering_opt_out_allows_unpause_during_grace_period(
        self, mock_apply_policy_actions
    ):
        """Resource with supports_pausing=False should be unpaused even during grace period."""
        mock_apply_policy_actions.return_value = True
        self.offering.plugin_options = {"supports_pausing": False}
        self.offering.save()
        self.resource.paused = True
        self.resource.save()

        with patch.object(
            type(self.project),
            "is_in_grace_period",
            new_callable=lambda: property(lambda self: True),
        ):
            tasks.evaluate_resource_against_policy(
                str(self.resource.uuid), str(self.policy.uuid)
            )

        self.resource.refresh_from_db()
        self.assertFalse(self.resource.paused)
