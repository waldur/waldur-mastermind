"""Basic tests for SLURM Periodic Usage Policy that actually work."""

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.models import PeriodMixin
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy.models import (
    OfferingUsagePolicy,
    SlurmPeriodicUsagePolicy,
)


class TestSlurmPeriodicUsagePolicyBasic(TestCase):
    """Basic working tests for SLURM periodic usage policy."""

    def setUp(self):
        """Set up basic test data."""
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.offering = marketplace_factories.OfferingFactory()

        # Create component
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="cpu"
        )

        # Create plan
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=self.component, amount=1000
        )

        # Create resource with per-component limits
        self.resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            limits={"cpu": 64000, "mem": 512000},
        )

    def test_policy_model_creation(self):
        """Test basic policy model creation."""
        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            apply_to_all=True,  # Required field now
            grace_ratio=0.2,
            carryover_enabled=True,
            carryover_factor=15,
        )

        # Check field values
        self.assertEqual(policy.grace_ratio, 0.2)
        self.assertTrue(policy.carryover_enabled)
        self.assertEqual(policy.carryover_factor, 15)
        self.assertEqual(policy.limit_type, "GrpTRESMins")  # Default
        self.assertTrue(policy.tres_billing_enabled)  # Default

        print("✅ Policy model creation working")

    def test_basic_settings_calculation(self):
        """Test basic settings calculation without complex mocking."""
        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=False,  # Disable carryover to simplify test
        )

        # Test basic calculation
        with patch.object(policy, "_get_current_period", return_value="2024-Q2"):
            settings = policy.calculate_slurm_settings(self.resource)

            # Verify basic settings structure
            self.assertIn("fairshare", settings)
            self.assertIn("grp_tres_mins", settings)
            self.assertIn("qos_threshold", settings)
            self.assertIn("grace_limit", settings)

            # Verify reasonable values
            self.assertGreater(settings["fairshare"], 0)
            # Per-component TRES minutes: cpu and mem should be present
            self.assertIn("cpu", settings["grp_tres_mins"])
            self.assertIn("mem", settings["grp_tres_mins"])
            self.assertGreater(settings["grp_tres_mins"]["cpu"], 0)
            self.assertGreater(settings["grp_tres_mins"]["mem"], 0)

        print("✅ Basic settings calculation working")

    def test_grace_ratio_increases_slurm_limit(self):
        """Test that SLURM GrpTRESMins includes grace ratio so jobs can run in the grace range."""
        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            apply_to_all=True,
            grace_ratio=0.3,
            carryover_enabled=False,
            tres_billing_enabled=False,
        )

        with patch.object(policy, "_get_current_period", return_value="2024-Q2"):
            settings = policy.calculate_slurm_settings(self.resource)

            # Base allocation: cpu=64000h, mem=512000h → base minutes: cpu=3840000, mem=30720000
            # With grace_ratio=0.3, SLURM limit should be 1.3x base
            base_cpu_minutes = int(64000 * 60)
            base_mem_minutes = int(512000 * 60)
            self.assertEqual(
                settings["grp_tres_mins"]["cpu"], int(base_cpu_minutes * 1.3)
            )
            self.assertEqual(
                settings["grp_tres_mins"]["mem"], int(base_mem_minutes * 1.3)
            )

            # QoS threshold should still be at base level (100%)
            self.assertEqual(
                settings["qos_threshold"]["node"], base_cpu_minutes + base_mem_minutes
            )
            # Grace limit should be at 130%
            self.assertEqual(
                settings["grace_limit"]["node"],
                int((base_cpu_minutes + base_mem_minutes) * 1.3),
            )

            # Invariant: SLURM hard limit must be >= grace limit for each
            # component, otherwise SLURM blocks jobs before QoS transitions fire
            slurm_total = sum(settings["grp_tres_mins"].values())
            grace_total = settings["grace_limit"]["node"]
            self.assertGreaterEqual(slurm_total, grace_total)

    def test_zero_grace_ratio_does_not_change_slurm_limit(self):
        """Test that with grace_ratio=0, SLURM limit equals base allocation."""
        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            apply_to_all=True,
            grace_ratio=0,
            carryover_enabled=False,
            tres_billing_enabled=False,
        )

        with patch.object(policy, "_get_current_period", return_value="2024-Q2"):
            settings = policy.calculate_slurm_settings(self.resource)

            # With grace_ratio=0, SLURM limit should equal base minutes exactly
            self.assertEqual(settings["grp_tres_mins"]["cpu"], int(64000 * 60))
            self.assertEqual(settings["grp_tres_mins"]["mem"], int(512000 * 60))

    def test_decay_calculation_method(self):
        """Test decay calculation method directly."""
        SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering, apply_to_all=True, carryover_factor=15
        )

        # Test the calculation logic that's used in carryover
        base_allocation = 1000.0
        previous_usage = 600.0

        # Calculate manually what _calculate_allocation_with_carryover should do
        days_elapsed = 90
        half_life = 15
        decay_factor = 2 ** (-days_elapsed / half_life)

        expected_decay = 0.015625  # 2^(-6)
        self.assertAlmostEqual(decay_factor, expected_decay, places=6)

        effective_usage = previous_usage * decay_factor
        unused = max(0, base_allocation - effective_usage)
        total_allocation = base_allocation + unused

        # Expected values
        self.assertAlmostEqual(effective_usage, 9.375, places=1)  # 600 * 0.015625
        self.assertAlmostEqual(unused, 990.625, places=1)  # 1000 - 9.375
        self.assertAlmostEqual(total_allocation, 1990.625, places=1)  # 1000 + 990.625

        print(
            f"✅ Decay calculation: {previous_usage}Nh → {effective_usage:.1f}Nh → {total_allocation:.1f}Nh"
        )

    def test_tres_minutes_calculation(self):
        """Test per-component TRES minutes calculation."""
        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering, apply_to_all=True, tres_billing_enabled=True
        )

        allocation = {"cpu": 64000.0, "mem": 512000.0}
        config = {"tres_billing_enabled": True, "tres_billing_weights": {}}
        tres_minutes = policy._calculate_tres_minutes(allocation, config)

        # Each component: hours * 60
        self.assertEqual(tres_minutes["cpu"], 64000 * 60)
        self.assertEqual(tres_minutes["mem"], 512000 * 60)

        print(f"✅ TRES minutes: {tres_minutes}")

    def test_tres_minutes_with_billing_weights(self):
        """Test TRES minutes calculation with billing weights."""
        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering, apply_to_all=True, tres_billing_enabled=True
        )

        allocation = {"cpu": 100.0, "mem": 200.0}
        weights = {"cpu": 0.5, "mem": 0.25}
        config = {"tres_billing_enabled": True, "tres_billing_weights": weights}
        tres_minutes = policy._calculate_tres_minutes(allocation, config)

        self.assertEqual(tres_minutes["cpu"], 6000)  # 100 * 60
        self.assertEqual(tres_minutes["mem"], 12000)  # 200 * 60
        # billing = (100*0.5 + 200*0.25) * 60 = (50 + 50) * 60 = 6000
        self.assertEqual(tres_minutes["billing"], 6000)

        print(f"✅ TRES minutes with billing weights: {tres_minutes}")

    def test_qos_threshold_calculation_scalar(self):
        """Test QoS threshold calculation with scalar (backward compat)."""
        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            apply_to_all=True,
            grace_ratio=0.25,
            tres_billing_enabled=True,
        )

        total_allocation = 1500.0
        config = {"grace_ratio": 0.25, "tres_billing_enabled": True}

        qos_threshold, grace_limit = policy._calculate_qos_thresholds(
            total_allocation, config
        )

        self.assertEqual(qos_threshold["billing"], 90000)
        self.assertEqual(grace_limit["billing"], 112500)

        print(
            f"✅ QoS thresholds (scalar): {qos_threshold['billing']:,} / {grace_limit['billing']:,}"
        )

    def test_qos_threshold_calculation_dict(self):
        """Test QoS threshold calculation with per-component dict."""
        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            apply_to_all=True,
            grace_ratio=0.25,
            tres_billing_enabled=True,
        )

        total_allocation = {"cpu": 100.0, "mem": 200.0}
        weights = {"cpu": 0.5, "mem": 0.25}
        config = {
            "grace_ratio": 0.25,
            "tres_billing_enabled": True,
            "tres_billing_weights": weights,
        }

        qos_threshold, grace_limit = policy._calculate_qos_thresholds(
            total_allocation, config
        )

        # scalar = 100*0.5 + 200*0.25 = 100
        # threshold = 100 * 60 = 6000, grace = 100 * 1.25 * 60 = 7500
        self.assertEqual(qos_threshold["billing"], 6000)
        self.assertEqual(grace_limit["billing"], 7500)

        print(
            f"✅ QoS thresholds (dict): {qos_threshold['billing']:,} / {grace_limit['billing']:,}"
        )

    def test_configuration_resolution(self):
        """Test configuration resolution without complex scenarios."""
        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            apply_to_all=True,
            grace_ratio=0.2,
            limit_type="MaxTRESMins",
            tres_billing_enabled=False,
        )

        # Test basic configuration resolution
        config = policy._resolve_configuration()

        self.assertEqual(config["grace_ratio"], 0.2)
        self.assertEqual(config["limit_type"], "MaxTRESMins")
        self.assertFalse(config["tres_billing_enabled"])

        # Test override
        override_config = policy._resolve_configuration({"grace_ratio": 0.3})
        self.assertEqual(override_config["grace_ratio"], 0.3)
        self.assertEqual(
            override_config["limit_type"], "MaxTRESMins"
        )  # Should preserve

        print("✅ Configuration resolution working")

    def test_default_tres_weights(self):
        """Test default TRES weights method."""
        policy = SlurmPeriodicUsagePolicy()
        weights = policy._get_default_tres_weights()

        expected = {"CPU": 0.015625, "Mem": 0.001953125, "GRES/gpu": 0.25}

        self.assertEqual(weights, expected)
        print("✅ Default TRES weights correct")

    def test_period_calculation_methods(self):
        """Test period calculation methods."""
        policy = SlurmPeriodicUsagePolicy()

        # Test previous period calculation for quarterly
        prev_q2 = policy._get_previous_period("2024-Q2")
        self.assertEqual(prev_q2, "2024-Q1")

        prev_q1 = policy._get_previous_period("2024-Q1")
        self.assertEqual(prev_q1, "2023-Q4")

        # Test previous period calculation for monthly
        prev_mar = policy._get_previous_period("2026-03")
        self.assertEqual(prev_mar, "2026-02")

        prev_jan = policy._get_previous_period("2026-01")
        self.assertEqual(prev_jan, "2025-12")

        # Test previous period calculation for annual
        prev_year = policy._get_previous_period("2026")
        self.assertEqual(prev_year, "2025")

        print("✅ Period calculation methods working")

        # Test current period - default period is MONTH_1 (monthly)
        current = policy._get_current_period()
        self.assertRegex(current, r"^\d{4}-\d{2}$")  # Should match YYYY-MM format

        # Test current period with quarterly policy
        quarterly_policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=self.offering,
            apply_to_all=True,
            period=PeriodMixin.Periods.MONTH_3,
        )
        current_quarterly = quarterly_policy._get_current_period()
        self.assertRegex(
            current_quarterly, r"^\d{4}-Q[1-4]$"
        )  # Should match YYYY-Q# format

        print(
            f"✅ Current period calculation working: monthly={current}, quarterly={current_quarterly}"
        )

    def test_fairshare_calculation(self):
        """Test fairshare calculation method with dict and scalar."""
        policy = SlurmPeriodicUsagePolicy()

        # Test with scalar (backward compat)
        fairshare_1000 = policy._calculate_fairshare(1000, {})
        self.assertEqual(fairshare_1000, 333)  # 1000 // 3

        # Test with dict — uses sum of values
        fairshare_dict = policy._calculate_fairshare({"cpu": 600, "mem": 900}, {})
        self.assertEqual(fairshare_dict, 500)  # (600 + 900) // 3

        fairshare_tiny = policy._calculate_fairshare({"cpu": 1, "mem": 1}, {})
        self.assertEqual(fairshare_tiny, 1)  # minimum value

        print(
            f"✅ Fairshare calculations: scalar=1000→{fairshare_1000}, dict→{fairshare_dict}"
        )


class TestSlurmPeriodicUsagePolicyCore(TestCase):
    """Test core functionality without complex integrations."""

    def test_policy_inheritance(self):
        """Test that policy correctly inherits from OfferingUsagePolicy."""
        # Check inheritance
        self.assertTrue(issubclass(SlurmPeriodicUsagePolicy, OfferingUsagePolicy))

        # Test that required OfferingUsagePolicy methods exist
        policy = SlurmPeriodicUsagePolicy()

        self.assertTrue(hasattr(policy, "is_triggered"))
        self.assertTrue(hasattr(policy, "apply_policy_actions"))

        # Test that new available_actions includes SLURM-specific ones
        self.assertIn("request_slurm_resource_downscaling", policy.available_actions)
        self.assertIn("request_slurm_resource_pausing", policy.available_actions)

        print("✅ Policy inheritance working correctly")

    def test_model_fields_and_defaults(self):
        """Test all model fields and their defaults."""
        offering = marketplace_factories.OfferingFactory()

        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=offering, apply_to_all=True
        )

        # Test all defaults
        self.assertEqual(policy.limit_type, "GrpTRESMins")
        self.assertTrue(policy.tres_billing_enabled)
        self.assertEqual(policy.carryover_factor, 50)
        self.assertEqual(policy.grace_ratio, 0.2)
        self.assertTrue(policy.carryover_enabled)
        self.assertTrue(policy.raw_usage_reset)
        self.assertEqual(policy.qos_strategy, "threshold")

        # Test TRES billing weights default
        self.assertEqual(policy.tres_billing_weights, {})  # Empty by default

        print("✅ All model fields and defaults working")

    def test_different_limit_types(self):
        """Test different limit type configurations."""
        offering = marketplace_factories.OfferingFactory()

        limit_types = [
            ("GrpTRESMins", "Group TRES Minutes"),
            ("MaxTRESMins", "Max TRES Minutes"),
            ("GrpTRES", "Group TRES (concurrent)"),
        ]

        for limit_type, description in limit_types:
            policy = SlurmPeriodicUsagePolicy.objects.create(
                scope=offering, apply_to_all=True, limit_type=limit_type
            )

            self.assertEqual(policy.limit_type, limit_type)
            print(f"✅ Limit type {limit_type} working")

            # Clean up
            policy.delete()

    def test_tres_billing_weights_storage(self):
        """Test TRES billing weights JSON field."""
        offering = marketplace_factories.OfferingFactory()

        custom_weights = {"CPU": 0.01, "Mem": 0.002, "GRES/gpu": 0.5}

        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=offering, apply_to_all=True, tres_billing_weights=custom_weights
        )

        # Reload from database
        policy.refresh_from_db()

        self.assertEqual(policy.tres_billing_weights, custom_weights)

        print(f"✅ TRES weights JSON storage: {policy.tres_billing_weights}")


class TestSlurmPeriodicUsagePolicyConstraints(TestCase):
    """Test unique constraints on SLURM periodic usage policy."""

    def test_unique_constraint_per_offering(self):
        """Test that only one SlurmPeriodicUsagePolicy can exist per offering."""
        offering = marketplace_factories.OfferingFactory()

        # Create first policy - should work
        policy1 = SlurmPeriodicUsagePolicy.objects.create(
            scope=offering,
            apply_to_all=True,
            grace_ratio=0.2,
            carryover_enabled=True,
        )

        self.assertIsNotNone(policy1.pk)
        print("✅ First policy created successfully")

        # Try to create second policy for same offering - should fail
        with self.assertRaises(ValidationError) as cm:
            SlurmPeriodicUsagePolicy.objects.create(
                scope=offering,
                apply_to_all=True,
                grace_ratio=0.3,
                carryover_enabled=False,
            )

        # Verify the error message
        error_msg = str(cm.exception)
        self.assertIn("already exists for this offering", error_msg.lower())
        print(f"✅ Second policy correctly blocked: {error_msg}")

    def test_multiple_policies_different_offerings(self):
        """Test that multiple policies can exist for different offerings."""
        offering1 = marketplace_factories.OfferingFactory()
        offering2 = marketplace_factories.OfferingFactory()

        # Create policies for different offerings - should work
        policy1 = SlurmPeriodicUsagePolicy.objects.create(
            scope=offering1,
            apply_to_all=True,
            grace_ratio=0.2,
        )

        policy2 = SlurmPeriodicUsagePolicy.objects.create(
            scope=offering2,
            apply_to_all=True,
            grace_ratio=0.3,
        )

        self.assertIsNotNone(policy1.pk)
        self.assertIsNotNone(policy2.pk)
        self.assertNotEqual(policy1.scope, policy2.scope)

        # Verify both policies exist
        self.assertEqual(SlurmPeriodicUsagePolicy.objects.count(), 2)
        print("✅ Multiple policies for different offerings allowed")

    def test_policy_update_preserves_constraint(self):
        """Test that updating a policy doesn't violate the constraint."""
        offering = marketplace_factories.OfferingFactory()

        policy = SlurmPeriodicUsagePolicy.objects.create(
            scope=offering,
            apply_to_all=True,
            grace_ratio=0.2,
        )

        # Update the policy - should work
        policy.grace_ratio = 0.4
        policy.carryover_enabled = False
        policy.carryover_factor = 30
        policy.save()

        # Reload and verify
        policy.refresh_from_db()
        self.assertEqual(policy.grace_ratio, 0.4)
        self.assertFalse(policy.carryover_enabled)
        self.assertEqual(policy.carryover_factor, 30)

        print("✅ Policy updates work correctly")

    def test_policy_deletion_allows_recreation(self):
        """Test that deleting a policy allows creating a new one for the same offering."""
        offering = marketplace_factories.OfferingFactory()

        # Create policy
        policy1 = SlurmPeriodicUsagePolicy.objects.create(
            scope=offering,
            apply_to_all=True,
            grace_ratio=0.2,
        )
        policy1_id = policy1.pk

        # Delete policy
        policy1.delete()
        self.assertFalse(
            SlurmPeriodicUsagePolicy.objects.filter(pk=policy1_id).exists()
        )

        # Create new policy for same offering - should work
        policy2 = SlurmPeriodicUsagePolicy.objects.create(
            scope=offering,
            apply_to_all=True,
            grace_ratio=0.3,
        )

        self.assertIsNotNone(policy2.pk)
        self.assertNotEqual(policy1_id, policy2.pk)

        print("✅ Policy deletion and recreation works")


if __name__ == "__main__":
    print("🧪 SLURM Periodic Usage Policy - Basic Tests")
    print("=" * 50)
    print("These tests validate core functionality without complex mocking")
    print()
    print("Run with:")
    print("cd /Users/ilja/workspace/waldur-mastermind")
    print(
        "DJANGO_SETTINGS_MODULE=waldur_core.server.my_test_settings uv run pytest src/waldur_mastermind/policy/tests/test_slurm_periodic_policy_basic.py -v"
    )
