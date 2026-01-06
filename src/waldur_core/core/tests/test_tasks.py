"""Tests for core Celery tasks and management commands."""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone


class CleanCeleryResultsManagementCommandTest(TransactionTestCase):
    """Tests for the clean_celery_results management command."""

    def setUp(self):
        """Create the celery_taskmeta table if it doesn't exist."""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS celery_taskmeta (
                    id SERIAL PRIMARY KEY,
                    task_id VARCHAR(255) UNIQUE,
                    status VARCHAR(50),
                    result BYTEA,
                    date_done TIMESTAMP WITH TIME ZONE,
                    traceback TEXT
                )
                """
            )

    def tearDown(self):
        """Clean up the test table."""
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS celery_taskmeta")

    def test_command_deletes_old_results(self):
        """Test that the management command deletes old results."""
        old_time = timezone.now() - timedelta(hours=25)
        recent_time = timezone.now() - timedelta(hours=1)

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO celery_taskmeta (task_id, status, date_done) VALUES (%s, %s, %s)",
                ["old-task-1", "SUCCESS", old_time],
            )
            cursor.execute(
                "INSERT INTO celery_taskmeta (task_id, status, date_done) VALUES (%s, %s, %s)",
                ["recent-task-1", "SUCCESS", recent_time],
            )

        out = StringIO()
        call_command("clean_celery_results", stdout=out)

        with connection.cursor() as cursor:
            cursor.execute("SELECT task_id FROM celery_taskmeta")
            remaining = [row[0] for row in cursor.fetchall()]

        self.assertNotIn("old-task-1", remaining)
        self.assertIn("recent-task-1", remaining)
        self.assertIn("Deleted 1 task results", out.getvalue())

    def test_command_dry_run_does_not_delete(self):
        """Test that dry-run mode doesn't delete anything."""
        old_time = timezone.now() - timedelta(hours=25)

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO celery_taskmeta (task_id, status, date_done) VALUES (%s, %s, %s)",
                ["old-task-1", "SUCCESS", old_time],
            )

        out = StringIO()
        call_command("clean_celery_results", "--dry-run", stdout=out)

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM celery_taskmeta")
            count = cursor.fetchone()[0]

        self.assertEqual(count, 1)  # Record should still exist
        self.assertIn("Would delete 1 task results", out.getvalue())

    def test_command_custom_hours(self):
        """Test that the command respects custom hours parameter."""
        # Create a result that's 5 hours old
        five_hours_ago = timezone.now() - timedelta(hours=5)

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO celery_taskmeta (task_id, status, date_done) VALUES (%s, %s, %s)",
                ["task-5h-old", "SUCCESS", five_hours_ago],
            )

        # With default 24 hours, nothing should be deleted
        out = StringIO()
        call_command("clean_celery_results", stdout=out)

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM celery_taskmeta")
            count = cursor.fetchone()[0]

        self.assertEqual(count, 1)

        # With 4 hours, it should be deleted
        out = StringIO()
        call_command("clean_celery_results", "--hours=4", stdout=out)

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM celery_taskmeta")
            count = cursor.fetchone()[0]

        self.assertEqual(count, 0)
        self.assertIn("Deleted 1 task results", out.getvalue())


class CleanCeleryResultsMissingTableTest(TestCase):
    """Test clean_celery_results command when celery_taskmeta table doesn't exist."""

    def test_command_handles_missing_table_gracefully(self):
        """Test that the command handles missing table gracefully."""
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS celery_taskmeta")

        out = StringIO()
        call_command("clean_celery_results", stdout=out)

        self.assertIn("does not exist", out.getvalue())
