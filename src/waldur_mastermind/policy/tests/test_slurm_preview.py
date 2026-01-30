"""Tests for SLURM policy preview calculations.

These are pure unit tests for standalone calculation functions.
"""

import unittest

from waldur_mastermind.policy import slurm_preview

# Use unittest.TestCase for pure function tests (no Django DB required)


class TestCarryover(unittest.TestCase):
    def test_full_carryover_with_no_usage(self):
        """When previous usage is zero, full allocation carries over (up to cap)."""
        result = slurm_preview.calculate_carryover(
            base_allocation=1000, previous_usage=0, carryover_factor=50
        )
        self.assertEqual(result["base_allocation"], 1000)
        # unused = 1000, cap = 500, carryover = 500
        self.assertEqual(result["unused"], 1000)
        self.assertEqual(result["carryover_cap"], 500)
        self.assertEqual(result["carryover"], 500)
        self.assertEqual(result["total_allocation"], 1500)

    def test_no_carryover_when_fully_used(self):
        """When all allocation was used, no carryover."""
        result = slurm_preview.calculate_carryover(
            base_allocation=1000, previous_usage=1000, carryover_factor=50
        )
        self.assertEqual(result["unused"], 0)
        self.assertEqual(result["carryover"], 0)
        self.assertEqual(result["total_allocation"], 1000)

    def test_partial_carryover(self):
        """Test partial usage results in partial carryover."""
        result = slurm_preview.calculate_carryover(
            base_allocation=1000, previous_usage=500, carryover_factor=50
        )
        # unused = 500, cap = 500, carryover = 500
        self.assertEqual(result["unused"], 500)
        self.assertEqual(result["carryover"], 500)
        self.assertEqual(result["total_allocation"], 1500)

    def test_carryover_capped_by_factor(self):
        """Carryover is capped by carryover_factor percentage of base."""
        result = slurm_preview.calculate_carryover(
            base_allocation=1000, previous_usage=0, carryover_factor=20
        )
        # unused = 1000, cap = 200, carryover = 200
        self.assertEqual(result["unused"], 1000)
        self.assertEqual(result["carryover_cap"], 200)
        self.assertEqual(result["carryover"], 200)
        self.assertEqual(result["total_allocation"], 1200)

    def test_zero_carryover_factor(self):
        """With 0% carryover factor, no carryover regardless of unused."""
        result = slurm_preview.calculate_carryover(
            base_allocation=1000, previous_usage=0, carryover_factor=0
        )
        self.assertEqual(result["carryover"], 0)
        self.assertEqual(result["total_allocation"], 1000)

    def test_100_percent_carryover_factor(self):
        """With 100% carryover factor, full unused amount carries over."""
        result = slurm_preview.calculate_carryover(
            base_allocation=1000, previous_usage=300, carryover_factor=100
        )
        # unused = 700, cap = 1000, carryover = 700
        self.assertEqual(result["carryover"], 700)
        self.assertEqual(result["total_allocation"], 1700)


class TestQosThresholds(unittest.TestCase):
    def test_default_thresholds(self):
        """Test default threshold calculations."""
        result = slurm_preview.preview_qos_thresholds(1000)
        self.assertEqual(result["allocation"], 1000)
        self.assertEqual(result["notification_threshold"], 800)  # 80%
        self.assertEqual(result["slowdown_threshold"], 1000)  # 100%
        self.assertEqual(result["blocked_threshold"], 1200)  # 120% (grace 20%)

    def test_custom_grace_ratio(self):
        """Test thresholds with custom grace ratio."""
        result = slurm_preview.preview_qos_thresholds(1000, grace_ratio=0.5)
        self.assertEqual(result["blocked_threshold"], 1500)  # 150%

    def test_custom_notification_ratio(self):
        """Test thresholds with custom notification ratio."""
        result = slurm_preview.preview_qos_thresholds(1000, notification_ratio=0.9)
        self.assertEqual(result["notification_threshold"], 900)  # 90%


class TestPolicyImpact(unittest.TestCase):
    def test_basic_impact_without_carryover(self):
        """Test policy impact without carryover enabled."""
        result = slurm_preview.preview_policy_impact(
            allocation=1000, carryover_enabled=False
        )
        self.assertEqual(result["base_allocation"], 1000)
        self.assertEqual(result["effective_allocation"], 1000)
        self.assertFalse(result["carryover_enabled"])
        self.assertIsNone(result["carryover"])
        self.assertIn("thresholds", result)

    def test_impact_with_carryover(self):
        """Test policy impact with carryover enabled."""
        result = slurm_preview.preview_policy_impact(
            allocation=1000,
            previous_usage=500,
            carryover_enabled=True,
            carryover_factor=50,
        )
        self.assertTrue(result["carryover_enabled"])
        self.assertIsNotNone(result["carryover"])
        # With 500 usage, unused=500, cap=500, carryover=500
        self.assertEqual(result["effective_allocation"], 1500)

    def test_impact_structure(self):
        """Test that all expected fields are in the result."""
        result = slurm_preview.preview_policy_impact(
            allocation=1000, carryover_enabled=True, previous_usage=100
        )
        expected_keys = [
            "base_allocation",
            "effective_allocation",
            "carryover_enabled",
            "carryover",
            "thresholds",
            "grace_ratio",
            "carryover_factor",
        ]
        for key in expected_keys:
            self.assertIn(key, result)


class TestTresBillingUnits(unittest.TestCase):
    def test_default_weights(self):
        """Test billing units with default weights."""
        result = slurm_preview.calculate_tres_billing_units(
            {"CPU": 64, "Mem": 512, "GRES/gpu": 4}
        )
        # 64 CPUs * 0.015625 = 1
        # 512 GB * 0.001953125 = 1
        # 4 GPUs * 0.25 = 1
        # Total = 3
        self.assertAlmostEqual(result, 3.0, places=4)

    def test_custom_weights(self):
        """Test billing units with custom weights."""
        result = slurm_preview.calculate_tres_billing_units(
            {"CPU": 100}, tres_weights={"CPU": 0.01}
        )
        self.assertAlmostEqual(result, 1.0, places=4)

    def test_partial_tres_types(self):
        """Test with only some TRES types."""
        result = slurm_preview.calculate_tres_billing_units(
            {"CPU": 128}  # Only CPU
        )
        # 128 * 0.015625 = 2
        self.assertAlmostEqual(result, 2.0, places=4)


class TestDaysUntilThreshold(unittest.TestCase):
    def test_already_exceeded(self):
        """When usage already exceeds threshold, return 0."""
        result = slurm_preview.calculate_days_until_threshold(
            current_usage=1200, daily_usage_rate=10, threshold=1000
        )
        self.assertEqual(result, 0)

    def test_zero_rate(self):
        """When rate is zero, return None (never reaches threshold)."""
        result = slurm_preview.calculate_days_until_threshold(
            current_usage=500, daily_usage_rate=0, threshold=1000
        )
        self.assertIsNone(result)

    def test_calculation(self):
        """Test proper calculation of days until threshold."""
        result = slurm_preview.calculate_days_until_threshold(
            current_usage=500, daily_usage_rate=50, threshold=1000
        )
        # (1000 - 500) / 50 = 10 days
        self.assertEqual(result, 10)
