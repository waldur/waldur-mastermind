"""Tests for SLURM policy preview calculations.

These are pure unit tests for standalone calculation functions.
"""

import unittest

from waldur_mastermind.policy import slurm_preview

# Use unittest.TestCase for pure function tests (no Django DB required)


class TestDecayFactor(unittest.TestCase):
    def test_zero_days_returns_one(self):
        """When no days have elapsed, decay factor should be 1.0 (no decay)."""
        result = slurm_preview.calculate_decay_factor(0, half_life=15)
        self.assertEqual(result, 1.0)

    def test_half_life_days_returns_half(self):
        """After half-life days, decay factor should be 0.5."""
        result = slurm_preview.calculate_decay_factor(15, half_life=15)
        self.assertAlmostEqual(result, 0.5, places=5)

    def test_double_half_life_returns_quarter(self):
        """After 2x half-life days, decay factor should be 0.25."""
        result = slurm_preview.calculate_decay_factor(30, half_life=15)
        self.assertAlmostEqual(result, 0.25, places=5)

    def test_custom_half_life(self):
        """Test with custom half-life value."""
        result = slurm_preview.calculate_decay_factor(30, half_life=30)
        self.assertAlmostEqual(result, 0.5, places=5)


class TestDecayImpact(unittest.TestCase):
    def test_no_decay_with_zero_days(self):
        """With zero days elapsed, effective usage equals previous usage."""
        result = slurm_preview.calculate_decay_impact(
            1000, days_elapsed=0, half_life=15
        )
        self.assertEqual(result["previous_usage"], 1000)
        self.assertEqual(result["effective_usage"], 1000)
        self.assertEqual(result["decay_factor"], 1.0)

    def test_half_decay_after_half_life(self):
        """After half-life days, effective usage should be halved."""
        result = slurm_preview.calculate_decay_impact(
            1000, days_elapsed=15, half_life=15
        )
        self.assertEqual(result["previous_usage"], 1000)
        self.assertAlmostEqual(result["effective_usage"], 500, places=1)
        self.assertAlmostEqual(result["decay_factor"], 0.5, places=5)

    def test_result_structure(self):
        """Test that all expected fields are in the result."""
        result = slurm_preview.calculate_decay_impact(
            500, days_elapsed=30, half_life=10
        )
        expected_keys = [
            "previous_usage",
            "days_elapsed",
            "half_life",
            "decay_factor",
            "effective_usage",
        ]
        for key in expected_keys:
            self.assertIn(key, result)


class TestCarryover(unittest.TestCase):
    def test_full_carryover_with_no_usage(self):
        """When previous usage is zero, full allocation carries over."""
        result = slurm_preview.calculate_carryover(
            base_allocation=1000, previous_usage=0, days_elapsed=90, half_life=15
        )
        self.assertEqual(result["base_allocation"], 1000)
        self.assertEqual(result["unused_carryover"], 1000)
        self.assertEqual(result["total_allocation"], 2000)

    def test_no_carryover_when_fully_used(self):
        """When all allocation was used (with no decay), no carryover."""
        # After 90 days with 15-day half-life, decay is very high
        # So effective usage would be very low, meaning high carryover
        result = slurm_preview.calculate_carryover(
            base_allocation=1000, previous_usage=1000, days_elapsed=0, half_life=15
        )
        # With 0 days elapsed, full usage means no unused
        self.assertEqual(result["unused_carryover"], 0)
        self.assertEqual(result["total_allocation"], 1000)

    def test_partial_carryover(self):
        """Test partial usage results in partial carryover."""
        result = slurm_preview.calculate_carryover(
            base_allocation=1000, previous_usage=500, days_elapsed=0, half_life=15
        )
        self.assertEqual(result["unused_carryover"], 500)
        self.assertEqual(result["total_allocation"], 1500)

    def test_carryover_with_decay(self):
        """Carryover calculation considers decay of previous usage."""
        result = slurm_preview.calculate_carryover(
            base_allocation=1000, previous_usage=1000, days_elapsed=15, half_life=15
        )
        # After 15 days, effective usage is 500, so unused is 500
        self.assertAlmostEqual(result["unused_carryover"], 500, places=1)
        self.assertAlmostEqual(result["total_allocation"], 1500, places=1)


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
            days_elapsed=0,
        )
        self.assertTrue(result["carryover_enabled"])
        self.assertIsNotNone(result["carryover"])
        # With 500 usage and no decay, carryover should be 500
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
            "half_life",
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
