"""
Tests for health check backends.
"""

from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase

from waldur_core.core.health_checks import CeleryWorkersHealthCheck


def _mock_connection(mock_app):
    """Wire mock_app.connection() to return a MagicMock context-manager conn."""
    mock_conn = MagicMock()
    mock_context = MagicMock()
    mock_context.__enter__ = MagicMock(return_value=mock_conn)
    mock_context.__exit__ = MagicMock(return_value=False)
    mock_app.connection.return_value = mock_context
    return mock_conn


class CeleryWorkersHealthCheckTest(TestCase):
    """Test the optimized Celery workers health check."""

    def test_successful_health_check(self):
        """Test health check passes when workers respond correctly."""
        backend = CeleryWorkersHealthCheck()

        # Mock discovery ping - returns list of workers
        discovery_response = [
            {"celery@worker1": {"ok": "pong"}},
            {"celery@worker2": {"ok": "pong"}},
        ]

        # Mock targeted pings - each worker responds
        targeted_responses = [
            [{"celery@worker1": {"ok": "pong"}}],
            [{"celery@worker2": {"ok": "pong"}}],
        ]

        with patch("waldur_core.core.health_checks.current_app") as mock_app:
            _mock_connection(mock_app)
            mock_app.control.ping.side_effect = [
                discovery_response
            ] + targeted_responses

            backend.check_status()

        self.assertEqual(len(backend.errors), 0)

    def test_no_workers_available(self):
        """Test health check fails when no workers respond."""
        backend = CeleryWorkersHealthCheck()

        with patch("waldur_core.core.health_checks.current_app") as mock_app:
            _mock_connection(mock_app)
            # Empty response means no workers
            mock_app.control.ping.return_value = []

            backend.check_status()

        self.assertEqual(len(backend.errors), 1)
        self.assertIn("No Celery workers", str(backend.errors[0]))

    def test_worker_not_responding(self):
        """Test health check fails when a discovered worker stops responding."""
        backend = CeleryWorkersHealthCheck()

        # Discovery finds workers
        discovery_response = [
            {"celery@worker1": {"ok": "pong"}},
        ]

        with patch("waldur_core.core.health_checks.current_app") as mock_app:
            _mock_connection(mock_app)
            # Discovery succeeds, but targeted ping fails
            mock_app.control.ping.side_effect = [discovery_response, []]

            backend.check_status()

        self.assertEqual(len(backend.errors), 1)
        self.assertIn("worker1", str(backend.errors[0]))
        self.assertIn("did not respond", str(backend.errors[0]))

    def test_worker_incorrect_response(self):
        """Test health check fails when worker responds incorrectly."""
        backend = CeleryWorkersHealthCheck()

        discovery_response = [{"celery@worker1": {"ok": "pong"}}]
        # Worker responds but with wrong content
        targeted_response = [{"celery@worker1": {"error": "something wrong"}}]

        with patch("waldur_core.core.health_checks.current_app") as mock_app:
            _mock_connection(mock_app)
            mock_app.control.ping.side_effect = [discovery_response, targeted_response]

            backend.check_status()

        self.assertEqual(len(backend.errors), 1)
        self.assertIn("responded incorrectly", str(backend.errors[0]))

    def test_broker_connection_error(self):
        """Test health check handles broker connection errors gracefully."""
        backend = CeleryWorkersHealthCheck()

        with patch("waldur_core.core.health_checks.current_app") as mock_app:
            mock_app.connection.side_effect = OSError("Connection refused")

            backend.check_status()

        self.assertEqual(len(backend.errors), 1)
        self.assertIn("Broker connection error", str(backend.errors[0]))

    def test_uses_fresh_non_pooled_connection(self):
        """Regression for CSCS-5B3.

        The check must use ``current_app.connection()`` (fresh, non-pooled)
        and must call ``ensure_connection`` to verify liveness before any
        publish. Using the broker connection pool can hand back sockets that
        the broker closed while sitting idle in the pool, producing
        BrokenPipe/ConnectionReset on the first publish.
        """
        backend = CeleryWorkersHealthCheck()

        with patch("waldur_core.core.health_checks.current_app") as mock_app:
            mock_conn = _mock_connection(mock_app)
            mock_app.control.ping.return_value = []

            backend.check_status()

            # Must NOT borrow from the pool
            self.assertFalse(mock_app.connection_or_acquire.called)
            # Must create a fresh connection
            mock_app.connection.assert_called_once()
            # Must validate liveness before publishing
            mock_conn.ensure_connection.assert_called_once_with(max_retries=1)

    def test_identifier(self):
        """Test health check identifier is correct."""
        backend = CeleryWorkersHealthCheck()
        self.assertEqual(backend.identifier(), "Celery Workers")

    def test_not_critical_service(self):
        """Test that Celery health check is not marked as critical."""
        backend = CeleryWorkersHealthCheck()
        # API should still be considered healthy even if workers are slow
        self.assertFalse(backend.critical_service)


class HealthCheckQueueConfigTest(TestCase):
    """Test that queue configuration is compatible with health checks."""

    def test_celery_queues_have_name_attribute(self):
        """Test that CELERY_TASK_QUEUES items have a name attribute."""
        defined_queues = settings.CELERY_TASK_QUEUES

        try:
            queue_names = {queue.name for queue in defined_queues}
            self.assertTrue(True, "Queue configuration is correct")
            self.assertEqual(
                queue_names,
                {"tasks-durable", "heavy-durable", "background-durable"},
                "All expected queues are present",
            )
        except AttributeError as e:
            self.fail(
                f"Health check queue processing failed with AttributeError: {e}\n"
                f"This means CELERY_TASK_QUEUES is not configured correctly."
            )
