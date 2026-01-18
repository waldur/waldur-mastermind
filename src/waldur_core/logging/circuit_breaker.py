"""Circuit breaker pattern implementation for RabbitMQ/STOMP connections.

The circuit breaker prevents cascading failures when RabbitMQ is unavailable
by failing fast instead of repeatedly attempting connections.

States:
- CLOSED: Normal operation, requests pass through
- OPEN: Failing, requests are rejected immediately
- HALF_OPEN: Testing if service has recovered
"""

import logging
import time
from enum import Enum
from threading import Lock

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker for RabbitMQ connections.

    Configuration:
        failure_threshold: Number of consecutive failures before opening circuit
        recovery_timeout: Seconds to wait before attempting recovery (half-open)
        success_threshold: Successful calls needed in half-open to close circuit

    Usage:
        if circuit_breaker.can_execute():
            try:
                # perform operation
                circuit_breaker.record_success()
            except Exception:
                circuit_breaker.record_failure()
                raise
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        success_threshold: int = 2,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._last_state_change: float | None = None
        self._state_history: list[dict] = []
        self._lock = Lock()

    def can_execute(self) -> bool:
        """Check if a request should be allowed to proceed.

        Returns:
            True if the circuit is closed or half-open, False if open.
        """
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                if self._should_attempt_recovery():
                    self._transition_to(
                        CircuitState.HALF_OPEN, "recovery_timeout_elapsed"
                    )
                    return True
                return False

            # HALF_OPEN: allow request to test if service recovered
            return True

    def record_success(self) -> None:
        """Record a successful operation."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._transition_to(
                        CircuitState.CLOSED, "success_threshold_reached"
                    )
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed operation."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # Failure during recovery test - reopen circuit
                self._transition_to(CircuitState.OPEN, "failure_during_recovery")
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._transition_to(CircuitState.OPEN, "failure_threshold_reached")

    def reset(self) -> None:
        """Manually reset circuit breaker to CLOSED state.

        Use with caution - only when RabbitMQ is confirmed healthy.
        """
        with self._lock:
            self._transition_to(CircuitState.CLOSED, "manual_reset")
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None

    def is_open(self) -> bool:
        """Check if circuit breaker is in OPEN state."""
        with self._lock:
            return self._state == CircuitState.OPEN

    def get_state(self) -> str:
        """Get current state as string."""
        with self._lock:
            return self._state.value

    def get_status(self) -> dict:
        """Get comprehensive status for debugging/monitoring."""
        with self._lock:
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "last_failure_time": self._last_failure_time,
                "last_state_change": self._last_state_change,
                "config": {
                    "failure_threshold": self.failure_threshold,
                    "recovery_timeout": self.recovery_timeout,
                    "success_threshold": self.success_threshold,
                },
                "state_history": list(self._state_history),
            }

    def _should_attempt_recovery(self) -> bool:
        """Check if enough time has passed to attempt recovery."""
        if self._last_failure_time is None:
            return True
        return time.time() - self._last_failure_time >= self.recovery_timeout

    def _transition_to(self, new_state: CircuitState, reason: str) -> None:
        """Transition to a new state and record the change.

        Must be called with lock held.
        """
        old_state = self._state
        self._state = new_state
        self._last_state_change = time.time()

        # Reset counters on state change
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._success_count = 0

        # Record state change for debugging
        self._state_history.append(
            {
                "timestamp": self._last_state_change,
                "from_state": old_state.value,
                "to_state": new_state.value,
                "reason": reason,
            }
        )

        # Keep only last 50 state changes
        if len(self._state_history) > 50:
            self._state_history = self._state_history[-50:]

        logger.info(
            "Circuit breaker state changed: %s -> %s (reason: %s)",
            old_state.value,
            new_state.value,
            reason,
        )


# Global circuit breaker instance for STOMP connections
stomp_circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60,
    success_threshold=2,
)
