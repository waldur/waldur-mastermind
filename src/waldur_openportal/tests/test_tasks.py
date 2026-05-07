from unittest import mock

from django.test import TestCase, override_settings

from waldur_core.structure.tests import factories as structure_factories
from waldur_openportal import tasks


class TaskConfigurationTest(TestCase):
    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
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

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
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

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.openportal.fetch_jobs")
    def test_sync_board_skips_when_config_unavailable(self, mock_fetch, mock_config):
        mock_config.return_value = False

        tasks.sync_board()

        mock_config.assert_called_once()
        mock_fetch.assert_not_called()

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.openportal.fetch_jobs")
    def test_sync_board_proceeds_when_config_available(self, mock_fetch, mock_config):
        mock_config.return_value = True
        mock_fetch.return_value = []

        tasks.sync_board()

        mock_config.assert_called_once()
        mock_fetch.assert_called_once()

    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
    def test_sync_offering_agents_logs_appropriate_message(
        self, mock_config, mock_logger
    ):
        mock_config.return_value = False

        tasks.sync_offering_agents()

        mock_logger.info.assert_called_once_with(
            "OpenPortal not enabled or config not available, skipping sync_offering_agents"
        )

    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
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

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
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

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
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

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
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

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
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

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
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

        # All three projects were processed
        self.assertEqual(mock_allocation_filter.call_count, 3)
        for i, proj in enumerate(projects):
            self.assertEqual(
                mock_allocation_filter.call_args_list[i],
                mock.call(project=proj, is_active=True),
            )

        # iterator() must NOT be called — it creates server-side cursors
        mock_qs.iterator.assert_not_called()


class SyncRemoteUsageTest(TestCase):
    """Tests for sync_remote_usage task with memory optimization."""

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.models.OnceTask.objects.get_or_create")
    @mock.patch("waldur_openportal.tasks.models.RemoteAllocation.objects.filter")
    def test_sync_remote_usage_eagerly_evaluates_queryset(
        self, mock_filter, mock_lock, mock_config
    ):
        """Test that sync_remote_usage evaluates the queryset eagerly
        to avoid server-side cursor issues (InvalidCursorName)."""
        # Setup mock config
        mock_config.return_value = True
        mock_lock.return_value = (mock.MagicMock(last_run=None), True)

        # Setup mock queryset chain
        mock_qs = mock.MagicMock()
        mock_qs.__iter__ = mock.Mock(return_value=iter([]))
        mock_filter.return_value = mock_qs

        tasks.sync_remote_usage()

        # Verify filter was called with is_active=True
        mock_filter.assert_called_once_with(is_active=True)
        # Verify iterator() was NOT called (no server-side cursor)
        mock_qs.iterator.assert_not_called()

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.sync_remote_allocation_usage")
    @mock.patch("waldur_openportal.tasks.models.OnceTask.objects.get_or_create")
    @mock.patch("waldur_openportal.tasks.models.RemoteAllocation.objects.filter")
    def test_sync_remote_usage_processes_allocations(
        self, mock_filter, mock_lock, mock_sync_usage, mock_config
    ):
        """Test that sync_remote_usage processes each allocation."""
        # Setup mock config
        mock_config.return_value = True
        mock_lock.return_value = (mock.MagicMock(last_run=None), True)

        # Create mock allocations
        allocation1 = mock.MagicMock()
        allocation2 = mock.MagicMock()

        mock_qs = mock.MagicMock()
        mock_qs.__iter__ = mock.Mock(return_value=iter([allocation1, allocation2]))
        mock_filter.return_value = mock_qs

        tasks.sync_remote_usage()

        # Verify sync was called for each allocation
        self.assertEqual(mock_sync_usage.call_count, 2)
        mock_sync_usage.assert_any_call(allocation1)
        mock_sync_usage.assert_any_call(allocation2)

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.sync_remote_allocation_usage")
    @mock.patch("waldur_openportal.tasks.models.OnceTask.objects.get_or_create")
    @mock.patch("waldur_openportal.tasks.models.RemoteAllocation.objects.filter")
    def test_sync_remote_usage_handles_errors_gracefully(
        self, mock_filter, mock_lock, mock_sync_usage, mock_logger, mock_config
    ):
        """Test that sync_remote_usage handles errors and continues processing."""
        # Setup mock config
        mock_config.return_value = True
        mock_lock.return_value = (mock.MagicMock(last_run=None), True)

        allocation1 = mock.MagicMock()
        allocation2 = mock.MagicMock()

        mock_qs = mock.MagicMock()
        mock_qs.__iter__ = mock.Mock(return_value=iter([allocation1, allocation2]))
        mock_filter.return_value = mock_qs

        # First call fails, second succeeds
        mock_sync_usage.side_effect = [Exception("Test error"), None]

        tasks.sync_remote_usage()

        # Both allocations should be attempted
        self.assertEqual(mock_sync_usage.call_count, 2)
        # Error should be logged
        mock_logger.error.assert_called()

    @mock.patch("waldur_openportal.tasks.openportal_config.ensure_config_loaded")
    @mock.patch("waldur_openportal.tasks.logger")
    @mock.patch("waldur_openportal.tasks.models.OnceTask.objects.get_or_create")
    @mock.patch("waldur_openportal.tasks.models.RemoteAllocation.objects.filter")
    def test_sync_remote_usage_logs_completion(
        self, mock_filter, mock_lock, mock_logger, mock_config
    ):
        """Test that sync_remote_usage logs completion with count."""
        # Setup mock config
        mock_config.return_value = True
        mock_lock.return_value = (mock.MagicMock(last_run=None), True)

        mock_qs = mock.MagicMock()
        mock_qs.__iter__ = mock.Mock(return_value=iter([]))
        mock_filter.return_value = mock_qs

        tasks.sync_remote_usage()

        # Check that completion log was called
        mock_logger.info.assert_any_call(
            "sync_remote_usage completed: processed 0 allocations"
        )
