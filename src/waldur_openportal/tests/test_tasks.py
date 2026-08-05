import datetime
import inspect
import re
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from waldur_core.structure.tests import factories as structure_factories
from waldur_openportal import models, tasks


class TaskConfigurationTest(TestCase):
    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.openportal.get_portal")
    @mock.patch("waldur_openportal.tasks.openportal.sync_offerings")
    @mock.patch("waldur_openportal.tasks.models.ProjectTemplate.objects.all")
    def test_sync_offering_agents_skips_when_config_unavailable(
        self, mock_templates, mock_sync, mock_portal, mock_config
    ):
        mock_config.return_value = False

        tasks.sync_offering_agents()

        mock_config.assert_called_once()
        mock_templates.assert_not_called()
        mock_portal.assert_not_called()
        mock_sync.assert_not_called()

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.openportal.get_portal")
    @mock.patch("waldur_openportal.tasks.openportal.sync_offerings")
    @mock.patch("waldur_openportal.tasks.models.ProjectTemplate.objects.all")
    def test_sync_offering_agents_proceeds_when_config_available(
        self, mock_templates, mock_sync, mock_portal, mock_config
    ):
        mock_config.return_value = True
        mock_portal.return_value = "test-portal"
        mock_templates.return_value = []

        tasks.sync_offering_agents()

        mock_config.assert_called_once()
        mock_templates.assert_called_once()
        mock_portal.assert_called_once()
        mock_sync.assert_called_once_with([])

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.openportal.fetch_jobs")
    def test_sync_board_skips_when_config_unavailable(self, mock_fetch, mock_config):
        mock_config.return_value = False

        tasks.sync_board()

        mock_config.assert_called_once()
        mock_fetch.assert_not_called()

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.openportal.fetch_jobs")
    def test_sync_board_proceeds_when_config_available(self, mock_fetch, mock_config):
        mock_config.return_value = True
        mock_fetch.return_value = []

        tasks.sync_board()

        mock_config.assert_called_once()
        mock_fetch.assert_called_once()

    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    def test_sync_offering_agents_logs_appropriate_message(
        self, mock_config, mock_logger
    ):
        mock_config.return_value = False

        tasks.sync_offering_agents()

        mock_logger.info.assert_called_once_with(
            "OpenPortal not enabled or config not available, skipping sync_offering_agents"
        )

    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    def test_sync_board_logs_appropriate_message(self, mock_config, mock_logger):
        mock_config.return_value = False

        tasks.sync_board()

        mock_logger.info.assert_called_once_with(
            "OpenPortal not enabled or config not available, skipping sync_board"
        )


class TaskIntegrationTest(TestCase):
    """Integration tests that verify the tasks work correctly with real settings"""

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    @mock.patch("waldur_openportal.tasks.models.ProjectTemplate.objects.all")
    def test_sync_offering_agents_with_disabled_setting(self, mock_templates):
        """Test that sync_offering_agents respects ENABLED=False setting"""
        tasks.sync_offering_agents()
        mock_templates.assert_not_called()

    @override_settings(WALDUR_OPENPORTAL={"ENABLED": False})
    @mock.patch("waldur_openportal.tasks.openportal.fetch_jobs")
    def test_sync_board_with_disabled_setting(self, mock_fetch):
        """Test that sync_board respects ENABLED=False setting"""
        tasks.sync_board()
        mock_fetch.assert_not_called()


class SyncAllocationLimitsTest(TestCase):
    """Tests for sync_allocation_limits task with memory optimization."""

    def setUp(self):
        # Create test project and customer
        self.project = structure_factories.ProjectFactory()

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.models.OnceTask.objects.get_or_create")
    @mock.patch("waldur_openportal.tasks.invoice_models.ProjectCredit.objects")
    def test_sync_allocation_limits_eagerly_evaluates_queryset(
        self, mock_queryset, mock_lock, mock_config
    ):
        """Test that sync_allocation_limits evaluates the queryset eagerly
        to avoid server-side cursor issues (InvalidCursorName)."""
        # Setup mock config
        mock_config.return_value = True
        # Setup lock to allow task execution
        mock_lock.return_value = (mock.MagicMock(last_run=None), True)

        # Setup mock queryset chain — list() calls __iter__ on the queryset
        mock_qs = mock.MagicMock()
        mock_qs.select_related.return_value = mock_qs
        mock_qs.__iter__ = mock.Mock(return_value=iter([]))
        mock_queryset.select_related.return_value = mock_qs

        tasks.sync_allocation_limits()

        # Verify select_related was called with "project"
        mock_queryset.select_related.assert_called_once_with("project")
        # Verify iterator() was NOT called (no server-side cursor)
        mock_qs.iterator.assert_not_called()

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.models.OnceTask.objects.get_or_create")
    @mock.patch("waldur_openportal.tasks.models.Allocation.objects.filter")
    @mock.patch("waldur_openportal.tasks.invoice_models.ProjectCredit.objects")
    def test_sync_allocation_limits_processes_active_projects(
        self, mock_credit_qs, mock_allocation_filter, mock_lock, mock_config
    ):
        """Test that sync_allocation_limits processes active projects correctly."""
        # Setup mock config
        mock_config.return_value = True
        mock_lock.return_value = (mock.MagicMock(last_run=None), True)

        # Create a mock project credit with an active project
        mock_project = mock.MagicMock()
        mock_project.is_removed = False
        mock_project.is_in_grace_period = False
        mock_project.is_expired = False

        mock_credit = mock.MagicMock()
        mock_credit.project = mock_project
        mock_credit.value = 100

        # Setup queryset chain
        mock_qs = mock.MagicMock()
        mock_qs.select_related.return_value = mock_qs
        mock_qs.__iter__ = mock.Mock(return_value=iter([mock_credit]))
        mock_credit_qs.select_related.return_value = mock_qs

        # No allocations for this project
        mock_allocation_filter.return_value = []

        tasks.sync_allocation_limits()

        # Verify allocation filter was called for the project
        mock_allocation_filter.assert_called_once_with(
            project=mock_project, is_active=True
        )

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.models.OnceTask.objects.get_or_create")
    @mock.patch("waldur_openportal.tasks.invoice_models.ProjectCredit.objects")
    def test_sync_allocation_limits_skips_removed_projects(
        self, mock_credit_qs, mock_lock, mock_config
    ):
        """Test that sync_allocation_limits skips removed projects."""
        # Setup mock config
        mock_config.return_value = True
        mock_lock.return_value = (mock.MagicMock(last_run=None), True)

        # Create a mock project credit with a removed project
        mock_project = mock.MagicMock()
        mock_project.is_removed = True

        mock_credit = mock.MagicMock()
        mock_credit.project = mock_project

        mock_qs = mock.MagicMock()
        mock_qs.select_related.return_value = mock_qs
        mock_qs.__iter__ = mock.Mock(return_value=iter([mock_credit]))
        mock_credit_qs.select_related.return_value = mock_qs

        with mock.patch(
            "waldur_openportal.tasks.models.Allocation.objects.filter"
        ) as mock_filter:
            tasks.sync_allocation_limits()
            # Should not query allocations for removed project
            mock_filter.assert_not_called()

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.models.OnceTask.objects.get_or_create")
    @mock.patch("waldur_openportal.tasks.invoice_models.ProjectCredit.objects")
    def test_sync_allocation_limits_logs_completion(
        self, mock_credit_qs, mock_lock, mock_logger, mock_config
    ):
        """Test that sync_allocation_limits logs completion with count."""
        # Setup mock config
        mock_config.return_value = True
        mock_lock.return_value = (mock.MagicMock(last_run=None), True)

        mock_qs = mock.MagicMock()
        mock_qs.select_related.return_value = mock_qs
        mock_qs.__iter__ = mock.Mock(return_value=iter([]))
        mock_credit_qs.select_related.return_value = mock_qs

        tasks.sync_allocation_limits()

        # Check that completion log was called
        mock_logger.info.assert_any_call(
            "sync_allocation_limits completed: processed 0 credits"
        )


class SyncAllocationLimitsCursorTest(TestCase):
    """Verify that sync_allocation_limits does not use server-side cursors,
    which are incompatible with PgBouncer transaction pooling and cause
    InvalidCursorName crashes (Sentry issue #28617).

    The queryset is eagerly evaluated via list() so all ProjectCredit
    rows are fetched before the loop begins. This avoids holding a
    server-side cursor open while the loop body makes slow external
    API calls and DB writes.
    """

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.models.OnceTask.objects.get")
    @mock.patch("waldur_openportal.tasks.models.OnceTask.objects.get_or_create")
    @mock.patch("waldur_openportal.tasks.models.Allocation.objects.filter")
    @mock.patch("waldur_openportal.tasks.invoice_models.ProjectCredit.objects")
    def test_all_projects_processed_without_server_side_cursor(
        self,
        mock_credit_qs,
        mock_allocation_filter,
        mock_lock_create,
        mock_lock_get,
        mock_config,
    ):
        """All project credits are processed because the queryset is eagerly
        evaluated, avoiding server-side cursor invalidation."""
        mock_config.return_value = True
        mock_lock_create.return_value = (mock.MagicMock(last_run=None), True)
        mock_lock_get.return_value = mock.MagicMock()

        # Create three mock project credits
        projects = []
        credits = []
        for i in range(3):
            proj = mock.MagicMock()
            proj.is_removed = False
            proj.is_in_grace_period = False
            proj.is_expired = False
            proj.name = f"project-{i}"
            projects.append(proj)

            credit = mock.MagicMock()
            credit.project = proj
            credit.value = 100
            credits.append(credit)

        mock_qs = mock.MagicMock()
        mock_qs.select_related.return_value = mock_qs
        mock_qs.__iter__ = mock.Mock(return_value=iter(credits))
        mock_credit_qs.select_related.return_value = mock_qs

        # No allocations for any project
        mock_allocation_filter.return_value = []

        tasks.sync_allocation_limits()

        # All three projects were processed. Processing order is randomised
        # (see random.shuffle in sync_allocation_limits), so check membership
        # rather than call order.
        self.assertEqual(mock_allocation_filter.call_count, 3)
        mock_allocation_filter.assert_has_calls(
            [mock.call(project=proj, is_active=True) for proj in projects],
            any_order=True,
        )

        # iterator() must NOT be called — it creates server-side cursors
        mock_qs.iterator.assert_not_called()


class SyncRemoteUsageTest(TestCase):
    """Tests for the sync_remote_usage dispatcher task, which fans out one
    sync_remote_usage_for_destination subtask per active destination so a
    down destination cannot block usage syncs for others."""

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.sync_remote_usage_for_destination")
    @mock.patch("waldur_openportal.tasks.models.RemoteAllocation.objects.filter")
    def test_sync_remote_usage_eagerly_evaluates_queryset(
        self, mock_filter, mock_dispatch, mock_config
    ):
        """Test that sync_remote_usage evaluates the destination id list eagerly
        to avoid server-side cursor issues (InvalidCursorName)."""
        mock_config.return_value = True
        mock_qs = mock.MagicMock()
        mock_qs.values_list.return_value.distinct.return_value = []
        mock_filter.return_value = mock_qs

        tasks.sync_remote_usage()

        mock_filter.assert_called_once_with(is_active=True)
        mock_qs.values_list.assert_called_once_with("service_settings_id", flat=True)

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.sync_remote_usage_for_destination")
    @mock.patch("waldur_openportal.tasks.models.RemoteAllocation.objects.filter")
    def test_sync_remote_usage_processes_allocations(
        self, mock_filter, mock_dispatch, mock_config
    ):
        """Test that sync_remote_usage dispatches one subtask per distinct destination."""
        mock_config.return_value = True
        mock_qs = mock.MagicMock()
        mock_qs.values_list.return_value.distinct.return_value = [1, 2]
        mock_filter.return_value = mock_qs

        tasks.sync_remote_usage()

        self.assertEqual(mock_dispatch.delay.call_count, 2)
        mock_dispatch.delay.assert_any_call(1)
        mock_dispatch.delay.assert_any_call(2)

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.sync_remote_usage_for_destination")
    @mock.patch("waldur_openportal.tasks.models.RemoteAllocation.objects.filter")
    def test_sync_remote_usage_handles_errors_gracefully(
        self, mock_filter, mock_dispatch, mock_logger, mock_config
    ):
        """Test that sync_remote_usage logs and continues if dispatching a
        subtask for one destination fails."""
        mock_config.return_value = True
        mock_qs = mock.MagicMock()
        mock_qs.values_list.return_value.distinct.return_value = [1, 2]
        mock_filter.return_value = mock_qs

        # First dispatch fails, second succeeds
        mock_dispatch.delay.side_effect = [Exception("Test error"), None]

        tasks.sync_remote_usage()

        # Both destinations should be attempted
        self.assertEqual(mock_dispatch.delay.call_count, 2)
        # Error should be logged
        mock_logger.error.assert_called()

    @mock.patch("waldur_openportal.tasks.config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.sync_remote_usage_for_destination")
    @mock.patch("waldur_openportal.tasks.models.RemoteAllocation.objects.filter")
    def test_sync_remote_usage_logs_completion(
        self, mock_filter, mock_dispatch, mock_logger, mock_config
    ):
        """Test that sync_remote_usage logs how many destinations it dispatched to."""
        mock_config.return_value = True
        mock_qs = mock.MagicMock()
        mock_qs.values_list.return_value.distinct.return_value = []
        mock_filter.return_value = mock_qs

        tasks.sync_remote_usage()

        mock_logger.info.assert_any_call(
            "OpenPortal task.sync_remote_usage: dispatching sync for 0 destination(s)"
        )


class RunOnceTaskTest(TestCase):
    """
    The lock that stops the periodic sync tasks from overlapping.

    These cases deliberately do not mock ``OnceTask.objects`` - the other tests
    in this module do, which is why the lock body itself went untested and the
    elapsed-time defect below survived. The lock row is real here.
    """

    TIMEOUT = 60 * 60

    def setUp(self):
        self.calls = []

    def make_task(self, timeout=None, include_args=False, raises=None):
        @tasks.run_once_task(
            takeover_timeout=timeout if timeout is not None else self.TIMEOUT,
            include_args=include_args,
        )
        def sample_task(*args):
            self.calls.append(args)
            if raises is not None:
                raise raises
            return "done"

        return sample_task

    def locks(self):
        return models.OnceTask.objects.all()

    def hold_lock(self, task_name, age=None):
        """Stand in for another worker holding the lock, optionally a stale one."""
        last_run = timezone.now() - age if age else timezone.now()
        return models.OnceTask.objects.create(task_name=task_name, last_run=last_run)

    def test_the_task_runs_and_the_lock_is_released(self):
        self.make_task()()

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.locks().count(), 0)

    def test_the_lock_is_released_when_the_task_fails(self):
        """
        A task that raises must not leave its lock behind, or the failure would
        also block every later run until the takeover timeout.
        """
        with self.assertRaises(ValueError):
            self.make_task(raises=ValueError("boom"))()

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.locks().count(), 0)

    def test_a_task_already_running_is_skipped(self):
        self.hold_lock("openportal-run-once-sample_task")

        self.make_task()()

        self.assertEqual(self.calls, [])

    def test_a_stale_lock_is_taken_over(self):
        self.hold_lock(
            "openportal-run-once-sample_task", age=datetime.timedelta(hours=2)
        )

        self.make_task()()

        self.assertEqual(len(self.calls), 1)

    def test_a_lock_orphaned_for_a_whole_day_is_taken_over(self):
        """
        Regression test. The elapsed time was measured with ``timedelta.seconds``,
        the seconds-within-the-day component rather than the total, so a lock
        orphaned for exactly 24 hours measured as 0 seconds old and 25 hours
        measured as 3600 - neither greater than the hour-long timeout. A task
        whose worker was killed was blocked for an hour after every 24-hour
        boundary.
        """
        for hours in (24, 25, 48, 49):
            with self.subTest(stale_for=f"{hours}h"):
                self.calls = []
                self.locks().delete()
                self.hold_lock(
                    "openportal-run-once-sample_task",
                    age=datetime.timedelta(hours=hours),
                )

                self.make_task()()

                self.assertEqual(len(self.calls), 1)

    def test_a_lock_dated_in_the_future_is_not_taken_over(self):
        """
        Clock skew between workers. A negative elapsed time must read as "held
        recently", not as a stale lock - which is what ``timedelta.seconds``
        would have made of it, since it normalises negatives into a large
        positive value.
        """
        self.hold_lock(
            "openportal-run-once-sample_task", age=-datetime.timedelta(hours=2)
        )

        self.make_task()()

        self.assertEqual(self.calls, [])

    def test_the_return_value_reaches_the_caller(self):
        self.assertEqual(self.make_task()(), "done")

    def test_a_skipped_task_returns_none(self):
        self.hold_lock("openportal-run-once-sample_task")

        self.assertIsNone(self.make_task()())

    def test_per_argument_locks_do_not_block_each_other(self):
        """
        The per-destination and per-customer tasks rely on this: one destination
        being synced must not stop another from starting.
        """
        task = self.make_task(include_args=True)

        task(1)
        task(2)

        self.assertEqual(self.calls, [(1,), (2,)])

    def test_a_per_argument_lock_blocks_the_same_argument(self):
        self.hold_lock("openportal-run-once-sample_task-1")

        self.make_task(include_args=True)(1)

        self.assertEqual(self.calls, [])

    def test_every_takeover_timeout_outlasts_the_celery_kill(self):
        """
        There is no heartbeat: the lock records when the task started and is
        never refreshed. That is only safe while Celery kills a task before its
        lock could be taken over from under it, so every timeout in the module
        has to stay above the hard time limit.
        """
        source = inspect.getsource(tasks)
        timeouts = [
            eval(m) for m in re.findall(r"takeover_timeout=([\d\s*]+?)[,)]", source)
        ]

        self.assertTrue(timeouts, "no takeover_timeout values found")
        for timeout in timeouts:
            self.assertGreater(timeout, settings.CELERY_TASK_TIME_LIMIT)
