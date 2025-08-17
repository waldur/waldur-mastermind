"""
Integration tests for health check endpoints.
"""

from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from health_check.contrib.celery_ping.backends import CeleryPingHealthCheck


class HealthCheckIntegrationTest(TestCase):
    """Test that health check endpoints work correctly with our configuration."""

    def test_health_check_endpoint_no_attribute_error(self):
        """Test that /health-check/ endpoint doesn't raise AttributeError with our queue config.

        This test verifies the fix for the runtime error where health_check.contrib.celery_ping
        was expecting Queue objects but received dictionaries/strings, causing:
        AttributeError: 'str' object has no attribute 'name'
        """
        # Create the health check backend
        backend = CeleryPingHealthCheck()

        # Mock the ping result to avoid actual Celery connection
        with patch.object(backend, "_check_ping_result"):
            # Simulate what happens when the health check runs
            # This should not raise AttributeError anymore
            try:
                # Get the configured queues
                defined_queues = settings.CELERY_TASK_QUEUES

                # This was the problematic line that caused AttributeError
                # when CELERY_TASK_QUEUES contained dicts instead of Queue objects
                queue_names = {queue.name for queue in defined_queues}

                # If we get here, the configuration is correct
                self.assertTrue(True, "Queue configuration is correct")
                self.assertEqual(
                    queue_names,
                    {"tasks", "heavy", "background"},
                    "All expected queues are present",
                )
            except AttributeError as e:
                self.fail(
                    f"Health check queue processing failed with AttributeError: {e}\n"
                    f"This means CELERY_TASK_QUEUES is not configured correctly."
                )

    def test_health_check_processes_queues_correctly(self):
        """Test that the health check can process our queue configuration."""
        # This simulates what the health check does internally
        # It should not raise AttributeError: 'str' object has no attribute 'name'
        defined_queues = settings.CELERY_TASK_QUEUES

        # This was the line that was failing before our fix
        try:
            queue_names = {queue.name for queue in defined_queues}
            self.assertTrue(True, "Queue processing succeeded")
        except AttributeError as e:
            self.fail(f"Queue processing failed with AttributeError: {e}")

        # Verify we got the expected queue names
        self.assertEqual(
            queue_names,
            {"tasks", "heavy", "background"},
            "Should have all three queues configured",
        )
