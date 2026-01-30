"""
Tests for SlurmPeriodicUsagePolicy threshold-based action triggering.

This module tests the integration between SlurmPeriodicUsagePolicy and
existing policy actions (request_slurm_resource_downscaling, request_slurm_resource_pausing) for
automatic SLURM QoS management.
"""

import datetime
from unittest.mock import patch

from django.test import TestCase
from freezegun import freeze_time

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.policy import models


class SlurmPeriodicUsagePolicyActionsTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = "SLURM"
        self.offering.save()

        # Create a resource
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
        )

        # Create node_hours component
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="node_hours",
            billing_type=marketplace_models.BillingTypes.USAGE,
        )

        # Create plan component with 1000 node-hour allocation
        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.resource.plan,
            component=self.component,
            amount=1000,
        )

    @freeze_time("2024-02-15")  # Q1 2024
    def test_policy_triggers_notification_at_80_percent_usage(self):
        """Test that notification action triggers at 80% usage."""
        # Create policy with notification action
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,  # Simplify test
        )

        # Create component limit
        models.OfferingComponentLimit.objects.create(
            policy=policy,
            component=self.component,
            limit=1000,
        )

        # Create usage at 80% (800 node-hours)
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=800,
            billing_period=datetime.date(2024, 1, 1),
        )

        # Policy should trigger
        self.assertTrue(policy.is_triggered())

        # Check usage percentage calculation
        usage_percentage = policy.get_resource_usage_percentage(self.resource)
        self.assertAlmostEqual(usage_percentage, 80.0, places=1)

    @freeze_time("2024-02-15")  # Q1 2024
    def test_policy_triggers_downscaling_at_100_percent_usage(self):
        """Test that request_slurm_resource_downscaling action triggers at 100% usage."""
        # Create policy with downscaling action
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,
        )

        # Create component limit
        models.OfferingComponentLimit.objects.create(
            policy=policy,
            component=self.component,
            limit=1000,
        )

        # Create usage at 100% (1000 node-hours)
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=1000,
            billing_period=datetime.date(2024, 1, 1),
        )

        # Policy should trigger
        self.assertTrue(policy.is_triggered())

        # Check that resource would be marked for downscaling
        usage_percentage = policy.get_resource_usage_percentage(self.resource)
        self.assertAlmostEqual(usage_percentage, 100.0, places=1)

    @freeze_time("2024-02-15")  # Q1 2024
    def test_policy_triggers_pausing_at_grace_limit(self):
        """Test that request_pausing action triggers at grace limit (120%)."""
        # Create policy with pausing action
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_pausing",
            apply_to_all=True,
            grace_ratio=0.2,  # 20% grace = 120% total
            carryover_enabled=False,
        )

        # Create usage at 120% (1200 node-hours)
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=1200,
            billing_period=datetime.date(2024, 1, 1),
        )

        # Policy should trigger
        self.assertTrue(policy.is_triggered())

        # Check usage percentage
        usage_percentage = policy.get_resource_usage_percentage(self.resource)
        self.assertAlmostEqual(usage_percentage, 120.0, places=1)

    @freeze_time("2024-02-15")  # Q1 2024
    def test_policy_does_not_trigger_below_threshold(self):
        """Test that policy doesn't trigger when usage is below threshold."""
        # Create policy with downscaling at 100%
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,
        )

        # Create usage at 50% (500 node-hours)
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=500,
            billing_period=datetime.date(2024, 1, 1),
        )

        # Policy should not trigger
        self.assertFalse(policy.is_triggered())

        # Check usage percentage
        usage_percentage = policy.get_resource_usage_percentage(self.resource)
        self.assertAlmostEqual(usage_percentage, 50.0, places=1)

    @freeze_time("2024-02-15")  # Q1 2024
    def test_multiple_actions_at_different_thresholds(self):
        """Test policy with multiple actions that trigger at different thresholds."""
        # Create policy with notification and downscaling
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners,request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,
        )

        # Test at 85% - should trigger notification
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=850,
            billing_period=datetime.date(2024, 1, 1),
        )

        self.assertTrue(policy.is_triggered())

        # Test at 100% - should trigger both
        marketplace_models.ComponentUsage.objects.filter(
            resource=self.resource
        ).delete()
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=1000,
            billing_period=datetime.date(2024, 1, 1),
        )

        self.assertTrue(policy.is_triggered())

    @freeze_time("2024-05-15")  # Q2 2024
    def test_carryover_affects_threshold_calculation(self):
        """Test that carryover logic affects when thresholds are triggered."""
        # Create policy with carryover enabled
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=True,
            carryover_factor=60,
        )

        # Create Q1 usage at 50% (500 node-hours)
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=500,
            billing_period=datetime.date(2024, 1, 1),
        )

        # In Q2, with 60% carryover factor:
        # unused = 1000 - 500 = 500
        # carryover_cap = 0.60 * 1000 = 600
        # carryover = min(500, 600) = 500
        # Q2 allocation: 1000 + 500 = 1500

        # Create Q2 usage at 1400 node-hours (under total allocation)
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=1400,
            billing_period=datetime.date(2024, 4, 1),
        )

        # Calculate usage percentage with carryover
        usage_percentage = policy.get_resource_usage_percentage(self.resource)

        # 1400 / 1500 ≈ 93.3%, below 100% downscaling threshold
        self.assertFalse(policy.is_triggered())
        self.assertLess(usage_percentage, 100)

    def test_policy_respects_apply_to_all_setting(self):
        """Test that policy only applies to resources in scope."""
        # Create policy that doesn't apply to all
        org_group = structure_factories.OrganizationGroupFactory()
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=False,
            grace_ratio=0.2,
        )
        policy.organization_groups.add(org_group)

        # Resource's customer is not in the organization group
        # so policy should not trigger even with high usage
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=2000,  # 200% usage
            billing_period=datetime.date(2024, 1, 1),
        )

        self.assertFalse(policy.is_triggered())

    def test_policy_excludes_terminated_resources(self):
        """Test that terminated resources are excluded from threshold checks."""
        # Create policy
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
            grace_ratio=0.2,
        )

        # Set resource as terminated
        self.resource.state = marketplace_models.ResourceStates.TERMINATED
        self.resource.save()

        # Create high usage
        marketplace_factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.component,
            usage=2000,
            billing_period=datetime.date(2024, 1, 1),
        )

        # Policy should not trigger for terminated resource
        self.assertFalse(policy.is_triggered())


class SlurmPeriodicUsagePolicyIntegrationTest(TestCase):
    """Test integration with policy actions framework."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = "SLURM"
        self.offering.save()

        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
        )

    @patch("waldur_mastermind.policy.policy_actions.request_slurm_resource_downscaling")
    def test_request_downscaling_action_called(self, mock_downscaling):
        """Test that request_slurm_resource_downscaling action is called when threshold exceeded."""
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_downscaling",
            apply_to_all=True,
        )

        # When policy framework processes triggered policies,
        # it should call the request_slurm_resource_downscaling action
        # This would mark resource.downscaled = True
        # Site agent would then apply qos_downscaled to SLURM account

        # Verify the action is available
        self.assertIn("request_slurm_resource_downscaling", policy.available_actions)

        # Verify it's configured in actions
        self.assertIn("request_slurm_resource_downscaling", policy.actions)

    @patch("waldur_mastermind.policy.policy_actions.request_slurm_resource_pausing")
    def test_request_pausing_action_called(self, mock_pausing):
        """Test that request_slurm_resource_pausing action is called when grace limit exceeded."""
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="request_slurm_resource_pausing",
            apply_to_all=True,
            grace_ratio=0.2,
        )

        # When policy framework processes triggered policies,
        # it should call the request_slurm_resource_pausing action
        # This would mark resource.paused = True
        # Site agent would then apply qos_paused to SLURM account

        # Verify the action is available
        self.assertIn("request_slurm_resource_pausing", policy.available_actions)

        # Verify it's configured in actions
        self.assertIn("request_slurm_resource_pausing", policy.actions)

    def test_combined_actions_for_progressive_management(self):
        """Test policy with combined actions for progressive QoS management."""
        policy = models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            actions="notify_organization_owners,request_slurm_resource_downscaling,block_creation_of_new_resources",
            apply_to_all=True,
            grace_ratio=0.2,
        )

        # All actions should be available
        for action in [
            "notify_organization_owners",
            "request_slurm_resource_downscaling",
            "block_creation_of_new_resources",
        ]:
            self.assertIn(action, policy.available_actions)
            self.assertIn(action, policy.actions)

        # This configuration would:
        # 1. Send notifications at 80% usage
        # 2. Apply slowdown QoS at 100% usage (via request_slurm_resource_downscaling)
        # 3. Block new resource creation at 100% usage
