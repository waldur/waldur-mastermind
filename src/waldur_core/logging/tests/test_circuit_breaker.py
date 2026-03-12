"""
Unit tests for the circuit breaker module.
These tests verify that the circuit breaker properly protects
against cascading failures in STOMP/RabbitMQ connections.

Note: Uses unittest.TestCase (not Django's) to avoid database dependencies.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from waldur_core.logging.circuit_breaker import CircuitBreaker, CircuitState
from waldur_core.logging.utils import publish_stomp_messages


class TestCircuitBreakerStateTransitions(unittest.TestCase):
    """Test circuit breaker state machine transitions."""

    def test_initial_state_is_closed(self):
        """Circuit starts in CLOSED state (normal operation)."""
        cb = CircuitBreaker()
        self.assertEqual(cb._state, CircuitState.CLOSED)

    def test_closed_allows_execution(self):
        """CLOSED state allows execution."""
        cb = CircuitBreaker()
        self.assertTrue(cb.can_execute())

    def test_transitions_to_open_after_threshold_failures(self):
        """Circuit opens after failure_threshold consecutive failures."""
        cb = CircuitBreaker(failure_threshold=3)

        # Record 3 failures
        for _ in range(3):
            cb.record_failure()

        self.assertEqual(cb._state, CircuitState.OPEN)

    def test_open_blocks_execution(self):
        """OPEN state blocks execution."""
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()

        self.assertFalse(cb.can_execute())

    def test_success_resets_failure_count(self):
        """Successful execution resets failure counter."""
        cb = CircuitBreaker(failure_threshold=5)

        # Record some failures (not enough to trip)
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb._failure_count, 2)

        # Success should reset
        cb.record_success()
        self.assertEqual(cb._failure_count, 0)

    def test_transitions_to_half_open_after_timeout(self):
        """Circuit transitions to HALF_OPEN after recovery_timeout."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()

        self.assertEqual(cb._state, CircuitState.OPEN)
        self.assertFalse(cb.can_execute())

        # Wait for recovery timeout
        time.sleep(0.15)

        # Should now allow test execution (half-open)
        self.assertTrue(cb.can_execute())
        self.assertEqual(cb._state, CircuitState.HALF_OPEN)

    def test_half_open_closes_after_success_threshold(self):
        """Circuit closes after success_threshold successes in HALF_OPEN."""
        cb = CircuitBreaker(
            failure_threshold=1, recovery_timeout=0, success_threshold=2
        )
        cb.record_failure()

        # Transition to half-open by checking can_execute after timeout
        cb.can_execute()
        self.assertEqual(cb._state, CircuitState.HALF_OPEN)

        # Record enough successes to close
        cb.record_success()
        cb.record_success()

        self.assertEqual(cb._state, CircuitState.CLOSED)

    def test_half_open_reopens_on_failure(self):
        """Circuit reopens on failure while in HALF_OPEN."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb.record_failure()

        # Transition to half-open
        cb.can_execute()
        self.assertEqual(cb._state, CircuitState.HALF_OPEN)

        # Failure in half-open reopens the circuit
        cb.record_failure()
        self.assertEqual(cb._state, CircuitState.OPEN)

    def test_reset_closes_circuit(self):
        """Manual reset closes the circuit."""
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        self.assertEqual(cb._state, CircuitState.OPEN)

        cb.reset()
        self.assertEqual(cb._state, CircuitState.CLOSED)
        self.assertEqual(cb._failure_count, 0)
        self.assertEqual(cb._success_count, 0)


class TestCircuitBreakerHistory(unittest.TestCase):
    """Test circuit breaker state history tracking."""

    def test_state_changes_are_recorded(self):
        """State changes are recorded in history."""
        cb = CircuitBreaker(failure_threshold=1)

        cb.record_failure()  # Opens circuit

        self.assertGreater(len(cb._state_history), 0)
        last_change = cb._state_history[-1]
        self.assertEqual(last_change["to_state"], CircuitState.OPEN.value)

    def test_history_limited_to_50_entries(self):
        """History is limited to prevent unbounded growth."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)

        # Trigger many state changes
        for _ in range(60):
            cb.record_failure()
            cb.reset()

        self.assertLessEqual(len(cb._state_history), 50)


class TestPublishStompMessagesCircuitBreakerRecovery(unittest.TestCase):
    """Test that publish_stomp_messages allows recovery after circuit breaker timeout."""

    def test_publish_attempts_recovery_after_timeout(self):
        """After recovery_timeout elapses, publish_stomp_messages should attempt
        to send messages instead of skipping them.

        Regression: is_open() was used as the early-return guard, which doesn't
        check recovery_timeout. This prevented the circuit breaker from ever
        transitioning to HALF_OPEN, making the OPEN state permanent.
        """
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        self.assertEqual(cb._state, CircuitState.OPEN)

        # Wait for recovery timeout
        time.sleep(0.15)

        messages = [
            {
                "vhost": "test_vhost",
                "topic": "test/topic",
                "payload": '{"key": "value"}',
            }
        ]

        rabbitmq_settings = {
            "HOST": "localhost",
            "STOMP_PORT": 61613,
            "USER": "guest",
            "PASSWORD": "guest",
        }

        with (
            patch("waldur_core.logging.circuit_breaker.stomp_circuit_breaker", cb),
            patch("waldur_core.logging.utils.settings") as mock_settings,
            patch("waldur_core.logging.utils.stomp.Connection12") as mock_conn_cls,
        ):
            mock_settings.RABBITMQ = rabbitmq_settings
            mock_conn = MagicMock()
            mock_conn_cls.return_value = mock_conn
            mock_conn.is_connected.return_value = True

            successful, failed = publish_stomp_messages(messages)

        # Should have attempted to send (not skipped)
        self.assertEqual(
            successful, 1, "Message should have been sent after recovery timeout"
        )
        self.assertEqual(failed, 0)


class TestGlobalCircuitBreaker(unittest.TestCase):
    """Test the global stomp_circuit_breaker instance."""

    def test_global_instance_exists(self):
        """Global circuit breaker instance is available."""
        from waldur_core.logging.circuit_breaker import stomp_circuit_breaker

        self.assertIsInstance(stomp_circuit_breaker, CircuitBreaker)

    def test_global_instance_has_correct_defaults(self):
        """Global instance has sensible default configuration."""
        from waldur_core.logging.circuit_breaker import stomp_circuit_breaker

        self.assertEqual(stomp_circuit_breaker.failure_threshold, 5)
        self.assertEqual(stomp_circuit_breaker.recovery_timeout, 60)
        self.assertEqual(stomp_circuit_breaker.success_threshold, 2)
