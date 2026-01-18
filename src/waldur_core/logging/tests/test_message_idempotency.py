"""
Unit tests for message idempotency and state tracking.
These tests verify that duplicate messages are properly detected
and skipped to prevent redundant processing.

Note: Uses unittest.TestCase with mocked cache to avoid database dependencies.
"""

import time
import unittest
from unittest import mock

from waldur_core.logging.utils import (
    MessageStateTracker,
    RateLimiter,
    get_next_sequence_number,
)


class TestMessageStateTracker(unittest.TestCase):
    """Test message state tracking for idempotency."""

    def setUp(self):
        """Set up mock cache for each test."""
        self.cache_data = {}
        self.cache_patcher = mock.patch("django.core.cache.cache")
        self.mock_cache = self.cache_patcher.start()
        self.mock_cache.get.side_effect = lambda k: self.cache_data.get(k)
        self.mock_cache.set.side_effect = (
            lambda k, v, *args, **kwargs: self.cache_data.__setitem__(k, v)
        )

    def tearDown(self):
        self.cache_patcher.stop()

    def test_first_message_should_be_sent(self):
        """First message for a resource should always be sent."""
        payload = {"resource_uuid": "abc123", "state": "OK"}

        result = MessageStateTracker.should_send_message("abc123", "resource", payload)

        self.assertTrue(result)

    def test_identical_payload_should_not_be_sent(self):
        """Identical payload should be skipped (duplicate)."""
        payload = {"resource_uuid": "abc123", "state": "OK"}

        # First send - should be allowed
        MessageStateTracker.should_send_message("abc123", "resource", payload)

        # Second send with same payload - should be skipped
        result = MessageStateTracker.should_send_message("abc123", "resource", payload)

        self.assertFalse(result)

    def test_different_payload_should_be_sent(self):
        """Changed payload should be sent."""
        payload1 = {"resource_uuid": "abc123", "state": "OK"}
        payload2 = {"resource_uuid": "abc123", "state": "ERRED"}

        MessageStateTracker.should_send_message("abc123", "resource", payload1)
        result = MessageStateTracker.should_send_message("abc123", "resource", payload2)

        self.assertTrue(result)

    def test_different_resources_tracked_separately(self):
        """Different resources have independent state tracking."""
        payload = {"state": "OK"}

        # Both should be sent as they're different resources
        result1 = MessageStateTracker.should_send_message(
            "resource-1", "resource", payload
        )
        result2 = MessageStateTracker.should_send_message(
            "resource-2", "resource", payload
        )

        self.assertTrue(result1)
        self.assertTrue(result2)

    def test_different_message_types_tracked_separately(self):
        """Different message types have independent state tracking."""
        payload = {"resource_uuid": "abc123"}

        result1 = MessageStateTracker.should_send_message("abc123", "resource", payload)
        result2 = MessageStateTracker.should_send_message("abc123", "order", payload)

        self.assertTrue(result1)
        self.assertTrue(result2)

    def test_hash_excludes_timestamp(self):
        """Timestamp field should be excluded from content hash."""
        payload1 = {
            "resource_uuid": "abc123",
            "state": "OK",
            "timestamp": "2024-01-01T00:00:00",
        }
        payload2 = {
            "resource_uuid": "abc123",
            "state": "OK",
            "timestamp": "2024-01-02T00:00:00",
        }

        MessageStateTracker.should_send_message("abc123", "resource", payload1)
        result = MessageStateTracker.should_send_message("abc123", "resource", payload2)

        # Should be skipped because only timestamp differs
        self.assertFalse(result)

    def test_hash_excludes_message_id(self):
        """Message ID field should be excluded from content hash."""
        payload1 = {"resource_uuid": "abc123", "state": "OK", "message_id": "msg-1"}
        payload2 = {"resource_uuid": "abc123", "state": "OK", "message_id": "msg-2"}

        MessageStateTracker.should_send_message("abc123", "resource", payload1)
        result = MessageStateTracker.should_send_message("abc123", "resource", payload2)

        # Should be skipped because only message_id differs
        self.assertFalse(result)

    def test_hash_excludes_sequence_number(self):
        """Sequence number field should be excluded from content hash."""
        payload1 = {"resource_uuid": "abc123", "state": "OK", "sequence_number": 1}
        payload2 = {"resource_uuid": "abc123", "state": "OK", "sequence_number": 2}

        MessageStateTracker.should_send_message("abc123", "resource", payload1)
        result = MessageStateTracker.should_send_message("abc123", "resource", payload2)

        # Should be skipped because only sequence_number differs
        self.assertFalse(result)


class TestSequenceNumbers(unittest.TestCase):
    """Test sequence number generation for message ordering."""

    def setUp(self):
        """Set up mock cache for sequence number tests."""
        self.cache_data = {}
        self.cache_patcher = mock.patch("waldur_core.logging.utils.cache")
        self.mock_cache = self.cache_patcher.start()

        def mock_incr(key):
            if key not in self.cache_data:
                raise ValueError("Key not found")
            self.cache_data[key] += 1
            return self.cache_data[key]

        self.mock_cache.incr.side_effect = mock_incr
        self.mock_cache.set.side_effect = (
            lambda k, v, **kwargs: self.cache_data.__setitem__(k, v)
        )

    def tearDown(self):
        self.cache_patcher.stop()

    def test_first_sequence_is_one(self):
        """First sequence number should be 1."""
        seq = get_next_sequence_number("resource-1", "resource")
        self.assertEqual(seq, 1)

    def test_sequence_increments(self):
        """Sequence numbers should increment monotonically."""
        seq1 = get_next_sequence_number("resource-1", "resource")
        seq2 = get_next_sequence_number("resource-1", "resource")
        seq3 = get_next_sequence_number("resource-1", "resource")

        self.assertEqual(seq1, 1)
        self.assertEqual(seq2, 2)
        self.assertEqual(seq3, 3)

    def test_different_resources_have_independent_sequences(self):
        """Different resources have separate sequence counters."""
        seq_a1 = get_next_sequence_number("resource-a", "resource")
        seq_b1 = get_next_sequence_number("resource-b", "resource")
        seq_a2 = get_next_sequence_number("resource-a", "resource")

        self.assertEqual(seq_a1, 1)
        self.assertEqual(seq_b1, 1)  # Independent counter
        self.assertEqual(seq_a2, 2)

    def test_different_message_types_have_independent_sequences(self):
        """Different message types have separate sequence counters."""
        seq_resource = get_next_sequence_number("abc123", "resource")
        seq_order = get_next_sequence_number("abc123", "order")

        self.assertEqual(seq_resource, 1)
        self.assertEqual(seq_order, 1)  # Independent counter


class TestRateLimiter(unittest.TestCase):
    """Test token bucket rate limiter."""

    def test_acquire_succeeds_with_available_tokens(self):
        """Acquire should succeed when tokens are available."""
        limiter = RateLimiter(rate=100, burst=100)

        # Should succeed with available tokens
        self.assertTrue(limiter.acquire(1))
        self.assertTrue(limiter.acquire(10))

    def test_acquire_fails_when_no_tokens(self):
        """Acquire should fail when tokens are exhausted."""
        limiter = RateLimiter(rate=1, burst=5)

        # Exhaust tokens
        for _ in range(5):
            limiter.acquire(1)

        # Should fail now
        self.assertFalse(limiter.acquire(1))

    def test_tokens_replenish_over_time(self):
        """Tokens should replenish based on rate over time."""
        limiter = RateLimiter(rate=100, burst=100)

        # Exhaust tokens
        limiter._tokens = 0

        # Wait a bit for tokens to replenish
        time.sleep(0.05)  # 50ms should give ~5 tokens at 100/sec

        # Should have some tokens now
        self.assertTrue(limiter.acquire(1))

    def test_burst_limits_max_tokens(self):
        """Burst should limit maximum token accumulation."""
        limiter = RateLimiter(rate=1000, burst=10)

        # Even with high rate, can't exceed burst
        time.sleep(0.1)

        # Should be capped at burst limit
        self.assertLessEqual(limiter._tokens, 10)
