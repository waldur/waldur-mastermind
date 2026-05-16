from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.core.cache import cache
from django.db import OperationalError
from django.test import TestCase, override_settings

from waldur_core.core.tasks import BackgroundTask


class DefaultTestTask(BackgroundTask):
    """
    A minimal implementation that uses the default get_unique_key logic, {}.
    We do NOT override anything here.
    """

    name = "waldur_core.core.tests.DefaultTestTask"

    def run(self, *args, **kwargs):
        pass


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class BackgroundTaskDefaultTest(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.task = DefaultTestTask()

    def test_task_with_args_acquires_lock(self):
        """
        Scenario: Task called with string arg 'instance:123'.
        Expected: Lock created, task scheduled.
        """
        arg = "instance:123"
        expected_key = self.task.get_unique_key((arg,), {})

        with mock.patch("celery.app.task.Task.apply_async") as mock_super_apply:
            mock_super_apply.return_value = mock.Mock(id="task-id-1")

            # Action
            self.task.apply_async(args=(arg,))

            # Assert Lock Exists
            self.assertIsNotNone(cache.get(expected_key), "Lock should be set in cache")

            # Assert Task Scheduled
            mock_super_apply.assert_called_once()

            # Assert Header Injection (Critical for cleanup)
            call_kwargs = mock_super_apply.call_args[1]
            self.assertEqual(call_kwargs["headers"]["__waldur_lock_key"], expected_key)

    def test_different_args_create_different_locks(self):
        """
        Scenario: 'instance:A' and 'instance:B' are scheduled.
        Expected: Both run, no collision.
        """
        arg_a = "instance:A"
        arg_b = "instance:B"
        key_a = self.task.get_unique_key((arg_a,), {})
        key_b = self.task.get_unique_key((arg_b,), {})

        with mock.patch("celery.app.task.Task.apply_async") as mock_super_apply:
            # Run A
            self.task.apply_async(args=(arg_a,))
            # Run B
            self.task.apply_async(args=(arg_b,))

            # Assert
            self.assertEqual(mock_super_apply.call_count, 2)
            self.assertIsNotNone(cache.get(key_a))
            self.assertIsNotNone(cache.get(key_b))
            self.assertNotEqual(key_a, key_b)

    def test_duplicate_args_are_skipped(self):
        """
        Scenario: 'instance:A' scheduled twice.
        Expected: Second call is skipped (deduplicated).
        """
        arg = "instance:A"
        expected_key = self.task.get_unique_key((arg,), {})

        # Simulate lock already taken by a running worker
        cache.set(expected_key, "running-task-id", timeout=60)

        with mock.patch("celery.app.task.Task.apply_async") as mock_super_apply:
            # Action
            result = self.task.apply_async(args=(arg,))

            # Assert
            mock_super_apply.assert_not_called()
            # We should still get an AsyncResult-like object back
            self.assertTrue(hasattr(result, "id"))

    def test_kwargs_are_ignored_by_default(self):
        """
        Scenario:
        1. Task('instance:123') is running.
        2. Task('instance:123', from_creation_date=True) is called.

        Expected:
        The default implementation (as proposed) should IGNORE kwargs to prevent
        the TypeError issue and logical duplicates. The second task should be SKIPPED.
        """
        arg = "instance:123"
        expected_key = self.task.get_unique_key((arg,), {})

        # 1. Lock exists (Standard pull running)
        cache.set(expected_key, "running-task", timeout=60)

        with mock.patch("celery.app.task.Task.apply_async") as mock_super_apply:
            # 2. Schedule specific pull
            self.task.apply_async(args=(arg,), kwargs={"from_creation_date": True})

            # Assert: It should be skipped because args match and we ignore kwargs
            mock_super_apply.assert_not_called()

    def test_cleanup_on_success(self):
        """
        Scenario: Worker finishes successfully.
        Expected: Lock deleted.
        """
        arg = "instance:123"
        lock_key = self.task.get_unique_key((arg,), {})
        cache.set(lock_key, "task-id", timeout=60)

        # Mock the request header context present during execution
        self.task.request.headers = {"__waldur_lock_key": lock_key}

        # Action
        self.task.after_return("SUCCESS", None, "task-id", (arg,), {}, None)

        # Assert
        self.assertIsNone(cache.get(lock_key), "Lock should be released")

    def test_cleanup_on_soft_time_limit_exceeded(self):
        """
        Scenario: Worker hits SoftTimeLimit.
        Expected: Lock deleted.
        """
        arg = "instance:123"
        lock_key = self.task.get_unique_key((arg,), {})
        cache.set(lock_key, "task-id", timeout=60)
        self.task.request.headers = {"__waldur_lock_key": lock_key}

        exc = SoftTimeLimitExceeded()

        # Action
        self.task.after_return("FAILURE", exc, "task-id", (arg,), {}, None)

        # Assert
        self.assertIsNone(cache.get(lock_key))

    def test_cleanup_on_broker_failure(self):
        """
        Scenario: Cache acquires lock, but RabbitMQ is down.
        Expected: Lock deleted immediately.
        """
        arg = "instance:123"
        lock_key = self.task.get_unique_key((arg,), {})

        with mock.patch("celery.app.task.Task.apply_async") as mock_super_apply:
            mock_super_apply.side_effect = Exception("Broker Down")

            with self.assertRaises(Exception):
                self.task.apply_async(args=(arg,))

            # Assert
            self.assertIsNone(
                cache.get(lock_key), "Lock should not persist if scheduling failed"
            )

    def test_no_args_handling(self):
        """
        Scenario: Task called with no arguments.
        Expected: Works fine, lock based on empty tuple.
        """
        expected_key = self.task.get_unique_key((), {})  # empty tuple arg, {}s

        with mock.patch("celery.app.task.Task.apply_async") as mock_super_apply:
            self.task.apply_async()

            self.assertIsNotNone(cache.get(expected_key))
            mock_super_apply.assert_called_once()

    def test_lock_expiration_allows_new_acquisition(self):
        """
        Scenario: Lock expires (TTL elapsed), then a new task is scheduled.
        Expected: New task acquires lock and runs.
        """
        arg = "instance:123"
        expected_key = self.task.get_unique_key((arg,), {})

        # Set lock with 0-second timeout so it expires immediately
        cache.set(expected_key, "old-task-id", timeout=0)

        with mock.patch("celery.app.task.Task.apply_async") as mock_super_apply:
            mock_super_apply.return_value = mock.Mock(id="new-task-id")

            self.task.apply_async(args=(arg,))

            # New task should have been scheduled
            mock_super_apply.assert_called_once()
            # Lock should now hold the new task's ID
            self.assertIsNotNone(cache.get(expected_key))

    def test_after_return_does_not_release_lock_owned_by_another_task(self):
        """
        Scenario: Lock expired and was re-acquired by another worker.
        The original task finishes and calls after_return.
        Expected: Lock is NOT deleted (it belongs to the new task).
        """
        arg = "instance:123"
        lock_key = self.task.get_unique_key((arg,), {})

        # Lock is now owned by a different task
        cache.set(lock_key, "new-task-id", timeout=60)
        self.task.request.headers = {"__waldur_lock_key": lock_key}

        # Original task (old-task-id) finishes
        self.task.after_return("SUCCESS", None, "old-task-id", (arg,), {}, None)

        # Lock should still exist and belong to the new task
        self.assertEqual(cache.get(lock_key), "new-task-id")

    def test_apply_async_recovers_from_stale_db_connection(self):
        """
        Scenario: celery-beat's long-lived DB connection is closed (idle
        timeout, DB restart, etc.). The first cache.add() raises
        OperationalError; the next call succeeds.

        Expected: apply_async retries the cache operation transparently and
        schedules the task instead of propagating the OperationalError to
        celery-beat (which would otherwise log a SchedulingError every tick
        until the pod is restarted).
        """
        arg = "instance:stale"
        expected_key = self.task.get_unique_key((arg,), {})

        cache_add = mock.Mock(
            side_effect=[OperationalError("the connection is closed"), True]
        )
        with (
            mock.patch("waldur_core.core.tasks.cache.add", cache_add),
            mock.patch("celery.app.task.Task.apply_async") as mock_super_apply,
        ):
            mock_super_apply.return_value = mock.Mock(id="task-id-stale")

            # Action — must not raise.
            self.task.apply_async(args=(arg,))

            # cache.add was retried after the OperationalError.
            self.assertEqual(cache_add.call_count, 2)
            # Task was scheduled (no OperationalError propagated to caller).
            mock_super_apply.assert_called_once()
            call_kwargs = mock_super_apply.call_args[1]
            self.assertEqual(call_kwargs["headers"]["__waldur_lock_key"], expected_key)
