from unittest import mock
from uuid import uuid4

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


def _fake_celery_apply_async(*args, **kwargs):
    """Mimic Celery: use the provided task_id or mint a new one."""
    task_id = kwargs.get("task_id")
    if task_id is None:
        task_id = str(uuid4())
    return mock.Mock(id=task_id)


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

        with mock.patch(
            "celery.app.task.Task.apply_async", side_effect=_fake_celery_apply_async
        ) as mock_super_apply:
            result = self.task.apply_async(args=(arg,))

            self.assertIsNotNone(cache.get(expected_key), "Lock should be set in cache")
            mock_super_apply.assert_called_once()

            call_kwargs = mock_super_apply.call_args[1]
            self.assertEqual(call_kwargs["headers"]["__waldur_lock_key"], expected_key)
            self.assertEqual(call_kwargs["task_id"], result.id)
            self.assertEqual(cache.get(expected_key), result.id)

    def test_different_args_create_different_locks(self):
        """
        Scenario: 'instance:A' and 'instance:B' are scheduled.
        Expected: Both run, no collision.
        """
        arg_a = "instance:A"
        arg_b = "instance:B"
        key_a = self.task.get_unique_key((arg_a,), {})
        key_b = self.task.get_unique_key((arg_b,), {})

        with mock.patch(
            "celery.app.task.Task.apply_async", side_effect=_fake_celery_apply_async
        ):
            self.task.apply_async(args=(arg_a,))
            self.task.apply_async(args=(arg_b,))

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

            mock_super_apply.assert_not_called()
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

        cache.set(expected_key, "running-task", timeout=60)

        with mock.patch("celery.app.task.Task.apply_async") as mock_super_apply:
            self.task.apply_async(args=(arg,), kwargs={"from_creation_date": True})

            mock_super_apply.assert_not_called()

    def test_cleanup_releases_lock_after_realistic_schedule(self):
        """
        Scenario: Beat schedules a task without an explicit task_id, the worker
        runs it, and after_return must clear the lock.

        This is the production path that broke when the cache stored one UUID
        while Celery executed the task under another.
        """
        arg = "instance:123"
        lock_key = self.task.get_unique_key((arg,), {})

        with mock.patch(
            "celery.app.task.Task.apply_async", side_effect=_fake_celery_apply_async
        ):
            result = self.task.apply_async(args=(arg,))

        self.assertEqual(cache.get(lock_key), result.id)

        self.task.request.headers = {"__waldur_lock_key": lock_key}
        self.task.after_return("SUCCESS", None, result.id, (arg,), {}, None)

        self.assertIsNone(cache.get(lock_key), "Lock should be released")

    def test_cleanup_on_soft_time_limit_exceeded(self):
        """
        Scenario: Worker hits SoftTimeLimit.
        Expected: Lock deleted.
        """
        arg = "instance:123"
        lock_key = self.task.get_unique_key((arg,), {})

        with mock.patch(
            "celery.app.task.Task.apply_async", side_effect=_fake_celery_apply_async
        ):
            result = self.task.apply_async(args=(arg,))

        self.task.request.headers = {"__waldur_lock_key": lock_key}
        exc = SoftTimeLimitExceeded()
        self.task.after_return("FAILURE", exc, result.id, (arg,), {}, None)

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

            self.assertIsNone(
                cache.get(lock_key), "Lock should not persist if scheduling failed"
            )

    def test_no_args_handling(self):
        """
        Scenario: Task called with no arguments.
        Expected: Works fine, lock based on empty tuple.
        """
        expected_key = self.task.get_unique_key((), {})

        with mock.patch(
            "celery.app.task.Task.apply_async", side_effect=_fake_celery_apply_async
        ) as mock_super_apply:
            result = self.task.apply_async()

            self.assertIsNotNone(cache.get(expected_key))
            self.assertEqual(cache.get(expected_key), result.id)
            mock_super_apply.assert_called_once()

    def test_lock_expiration_allows_new_acquisition(self):
        """
        Scenario: Lock expires (TTL elapsed), then a new task is scheduled.
        Expected: New task acquires lock and runs.
        """
        arg = "instance:123"
        expected_key = self.task.get_unique_key((arg,), {})

        cache.set(expected_key, "old-task-id", timeout=0)

        with mock.patch(
            "celery.app.task.Task.apply_async", side_effect=_fake_celery_apply_async
        ) as mock_super_apply:
            result = self.task.apply_async(args=(arg,))

            mock_super_apply.assert_called_once()
            self.assertEqual(cache.get(expected_key), result.id)

    def test_after_return_does_not_release_lock_owned_by_another_task(self):
        """
        Scenario: Lock expired and was re-acquired by another worker.
        The original task finishes and calls after_return.
        Expected: Lock is NOT deleted (it belongs to the new task).
        """
        arg = "instance:123"
        lock_key = self.task.get_unique_key((arg,), {})

        cache.set(lock_key, "new-task-id", timeout=60)
        self.task.request.headers = {"__waldur_lock_key": lock_key}

        self.task.after_return("SUCCESS", None, "old-task-id", (arg,), {}, None)

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
            mock.patch(
                "celery.app.task.Task.apply_async", side_effect=_fake_celery_apply_async
            ) as mock_super_apply,
        ):
            result = self.task.apply_async(args=(arg,))

            self.assertEqual(cache_add.call_count, 2)
            mock_super_apply.assert_called_once()
            call_kwargs = mock_super_apply.call_args[1]
            self.assertEqual(call_kwargs["headers"]["__waldur_lock_key"], expected_key)
            self.assertEqual(call_kwargs["task_id"], result.id)

    def test_apply_async_preserves_caller_provided_task_id(self):
        arg = "instance:explicit"
        expected_key = self.task.get_unique_key((arg,), {})
        explicit_id = str(uuid4())

        with mock.patch(
            "celery.app.task.Task.apply_async", side_effect=_fake_celery_apply_async
        ) as mock_super_apply:
            result = self.task.apply_async(args=(arg,), task_id=explicit_id)

            self.assertEqual(result.id, explicit_id)
            self.assertEqual(mock_super_apply.call_args[1]["task_id"], explicit_id)
            self.assertEqual(cache.get(expected_key), explicit_id)
