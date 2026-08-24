"""
Tests for Celery configuration to prevent runtime errors with health checks.
"""

from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from health_check.contrib.celery_ping.backends import CeleryPingHealthCheck
from kombu import Queue

from waldur_core.server.celeryconf import PriorityRouter, app


class CeleryConfigurationTest(TestCase):
    """Test that Celery queues are properly configured for health check compatibility."""

    def test_celery_task_queues_are_queue_objects(self):
        """Verify CELERY_TASK_QUEUES contains Queue objects, not dictionaries."""
        # This test ensures compatibility with django-health-check's celery_ping backend
        # which expects Queue objects with a 'name' attribute
        self.assertIsInstance(
            settings.CELERY_TASK_QUEUES,
            list,
            "CELERY_TASK_QUEUES must be a list",
        )

        for queue in settings.CELERY_TASK_QUEUES:
            self.assertIsInstance(
                queue,
                Queue,
                f"Each queue must be a kombu.Queue object, got {type(queue)}",
            )
            self.assertTrue(
                hasattr(queue, "name"),
                "Queue object must have a 'name' attribute for health check compatibility",
            )

    def test_default_queues_are_configured(self):
        """Verify that all expected queues are configured."""
        queue_names = {queue.name for queue in settings.CELERY_TASK_QUEUES}
        expected_queues = {"tasks-durable", "heavy-durable", "background-durable"}

        self.assertEqual(
            queue_names,
            expected_queues,
            f"Expected queues {expected_queues}, got {queue_names}",
        )

    def test_control_plane_queues_are_exclusive(self):
        """Verify the pidbox and gossip queues declare as exclusive, not plain transient.

        RabbitMQ 4.3 denies ``transient_nonexcl_queues`` by default; a worker
        declaring one dies with ``INTERNAL_ERROR (541)`` on startup. Exclusive
        transient queues are accepted by every supported broker version, so
        these two settings are what lets a single build run against 4.1, 4.2
        and 4.3 brokers without a broker-side permit flag.
        """
        self.assertTrue(
            settings.CELERY_CONTROL_QUEUE_EXCLUSIVE,
            "Pidbox/reply queues must be exclusive for RabbitMQ 4.3 compatibility",
        )
        self.assertTrue(
            settings.CELERY_EVENT_QUEUE_EXCLUSIVE,
            "Gossip/event queue must be exclusive for RabbitMQ 4.3 compatibility",
        )

        # Assert on what actually reaches the broker, not just the settings:
        # kombu builds the pidbox queues from the app config.
        mailbox = app.control.mailbox
        for queue in (mailbox.get_queue("worker@example"), mailbox.get_reply_queue()):
            self.assertTrue(
                queue.exclusive,
                f"Queue {queue.name} must be declared exclusive",
            )
            self.assertFalse(
                queue.durable,
                f"Queue {queue.name} is expected to stay transient",
            )

    def test_queue_exchanges_are_configured(self):
        """Verify that queues have proper exchange configuration."""
        for queue in settings.CELERY_TASK_QUEUES:
            self.assertIsNotNone(
                queue.exchange,
                f"Queue {queue.name} must have an exchange configured",
            )
            # In the default configuration, exchange name matches queue name
            self.assertEqual(
                queue.exchange.name,
                queue.name,
                f"Queue {queue.name} exchange name should match queue name",
            )

    def test_celery_default_queue_is_valid(self):
        """Verify the default queue is one of the configured queues."""
        queue_names = {queue.name for queue in settings.CELERY_TASK_QUEUES}
        self.assertIn(
            settings.CELERY_TASK_DEFAULT_QUEUE,
            queue_names,
            f"Default queue '{settings.CELERY_TASK_DEFAULT_QUEUE}' must be in configured queues",
        )

    @patch(
        "health_check.contrib.celery_ping.backends.CeleryPingHealthCheck.check_status"
    )
    def test_health_check_celery_ping_compatibility(self, mock_check_status):
        """Test that queue configuration is compatible with health check celery ping."""
        CeleryPingHealthCheck()

        # Simulate the health check's queue processing
        # This mimics the code that was failing before our fix
        defined_queues = settings.CELERY_TASK_QUEUES

        # This line was causing AttributeError: 'str' object has no attribute 'name'
        # when CELERY_TASK_QUEUES contained dictionaries instead of Queue objects
        queue_names = {queue.name for queue in defined_queues}

        # If we get here without an AttributeError, the configuration is correct
        self.assertIsInstance(queue_names, set)
        self.assertTrue(len(queue_names) > 0)

    def test_regression_dict_queues_cause_attribute_error(self):
        """Regression test: ensure dict-based queues would fail (documenting the bug we fixed)."""
        # This test documents the bug that was fixed
        # CELERY_TASK_QUEUES used to be defined as:
        # {"tasks": {"exchange": "tasks"}, "heavy": {"exchange": "heavy"}, ...}

        incorrect_queues = {
            "tasks": {"exchange": "tasks"},
            "heavy": {"exchange": "heavy"},
            "background": {"exchange": "background"},
        }

        # The health check code tries to do this:
        # queue_names = {queue.name for queue in defined_queues}

        # With dict values, this would work but give wrong results (queue names would be dict keys)
        if isinstance(incorrect_queues, dict):
            # This is what would happen with the old configuration
            queue_names = set(
                incorrect_queues.keys()
            )  # Would give {'tasks', 'heavy', 'background'}
            self.assertEqual(queue_names, {"tasks", "heavy", "background"})

        # But if the health check received the dict values as a list:
        incorrect_queue_list = list(incorrect_queues.values())

        # This would cause AttributeError: 'dict' object has no attribute 'name'
        with self.assertRaises(AttributeError) as context:
            {queue.name for queue in incorrect_queue_list}

        self.assertIn("has no attribute 'name'", str(context.exception))

    def test_priority_router_queue_mapping(self):
        """Test that the PriorityRouter correctly maps tasks to queues."""
        router = PriorityRouter()

        # Mock a heavy task
        class MockHeavyTask:
            is_heavy_task = True

        # Mock a background task
        class MockBackgroundTask:
            is_background = True

        # Mock a regular task
        class MockRegularTask:
            pass

        with patch("waldur_core.server.celeryconf.app.tasks.get") as mock_get:
            # Test heavy task routing
            mock_get.return_value = MockHeavyTask()
            result = router.route_for_task("heavy_task")
            self.assertEqual(result, {"queue": "heavy-durable"})

            # Test background task routing
            mock_get.return_value = MockBackgroundTask()
            result = router.route_for_task("background_task")
            self.assertEqual(result, {"queue": "background-durable"})

            # Test regular task routing (returns None, uses default queue)
            mock_get.return_value = MockRegularTask()
            result = router.route_for_task("regular_task")
            self.assertIsNone(result)
