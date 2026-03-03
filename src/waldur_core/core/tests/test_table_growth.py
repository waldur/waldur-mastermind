from datetime import timedelta
from unittest.mock import MagicMock, patch

from constance.test import override_config
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.core.models import DailyTableSizeHistory
from waldur_core.core.tasks import check_table_growth_alerts, sample_table_sizes
from waldur_core.structure.tests import fixtures


class DailyTableSizeHistoryModelTest(TestCase):
    """Tests for the DailyTableSizeHistory model."""

    def test_create_table_size_history(self):
        """Test creating a table size history entry."""
        today = timezone.now().date()
        entry = DailyTableSizeHistory.objects.create(
            table_name="test_table",
            date=today,
            total_size=1024 * 1024,  # 1MB
            data_size=512 * 1024,  # 512KB
            row_estimate=1000,
        )
        self.assertEqual(entry.table_name, "test_table")
        self.assertEqual(entry.date, today)
        self.assertEqual(entry.total_size, 1024 * 1024)
        self.assertEqual(entry.data_size, 512 * 1024)
        self.assertEqual(entry.row_estimate, 1000)

    def test_unique_together_constraint(self):
        """Test that table_name and date must be unique together."""
        today = timezone.now().date()
        DailyTableSizeHistory.objects.create(
            table_name="test_table",
            date=today,
            total_size=1024,
            data_size=512,
            row_estimate=100,
        )
        # Creating another entry with the same table_name and date should raise an error
        with self.assertRaises(IntegrityError):
            DailyTableSizeHistory.objects.create(
                table_name="test_table",
                date=today,
                total_size=2048,
                data_size=1024,
                row_estimate=200,
            )

    def test_str_representation(self):
        """Test the string representation of the model."""
        today = timezone.now().date()
        entry = DailyTableSizeHistory.objects.create(
            table_name="my_table",
            date=today,
            total_size=1024,
            data_size=512,
            row_estimate=100,
        )
        expected_str = f"my_table ({today})"
        self.assertEqual(str(entry), expected_str)

    def test_ordering(self):
        """Test that entries are ordered by date descending, then table_name."""
        today = timezone.now().date()
        yesterday = today - timedelta(days=1)

        DailyTableSizeHistory.objects.create(
            table_name="z_table",
            date=yesterday,
            total_size=1024,
            data_size=512,
            row_estimate=100,
        )
        DailyTableSizeHistory.objects.create(
            table_name="a_table",
            date=today,
            total_size=1024,
            data_size=512,
            row_estimate=100,
        )
        DailyTableSizeHistory.objects.create(
            table_name="b_table",
            date=today,
            total_size=1024,
            data_size=512,
            row_estimate=100,
        )

        entries = list(DailyTableSizeHistory.objects.all())
        # Today's entries should come first, ordered by table_name
        self.assertEqual(entries[0].table_name, "a_table")
        self.assertEqual(entries[1].table_name, "b_table")
        # Yesterday's entry last
        self.assertEqual(entries[2].table_name, "z_table")


class TableGrowthStatsAPITest(APITestCase):
    """Tests for the table growth stats API endpoint."""

    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.url = "/api/stats/table-growth/"
        self.today = timezone.now().date()
        self.week_ago = self.today - timedelta(days=7)
        self.month_ago = self.today - timedelta(days=30)

    def _create_test_data(self):
        """Create test data for growth analysis."""
        # Current data
        DailyTableSizeHistory.objects.create(
            table_name="growing_table",
            date=self.today,
            total_size=2000000,  # 2MB
            data_size=1500000,
            row_estimate=2000,
        )
        # Week-ago data (table was 1MB, now 2MB = 100% growth)
        DailyTableSizeHistory.objects.create(
            table_name="growing_table",
            date=self.week_ago,
            total_size=1000000,  # 1MB
            data_size=750000,
            row_estimate=1000,
        )
        # Month-ago data
        DailyTableSizeHistory.objects.create(
            table_name="growing_table",
            date=self.month_ago,
            total_size=500000,  # 0.5MB
            data_size=375000,
            row_estimate=500,
        )
        # Stable table for comparison
        DailyTableSizeHistory.objects.create(
            table_name="stable_table",
            date=self.today,
            total_size=1000000,
            data_size=750000,
            row_estimate=1000,
        )
        DailyTableSizeHistory.objects.create(
            table_name="stable_table",
            date=self.week_ago,
            total_size=1000000,
            data_size=750000,
            row_estimate=1000,
        )

    def test_anonymous_user_cannot_access(self):
        """Test that anonymous users cannot access the endpoint."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_access(self):
        """Test that regular users cannot access the endpoint."""
        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_user_can_access(self):
        """Test that staff users can access the endpoint."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_user_can_access(self):
        """Test that support users can access the endpoint."""
        support_user = self.fixture.user
        support_user.is_support = True
        support_user.save()
        self.client.force_authenticate(user=support_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_response_structure(self):
        """Test that response has expected structure."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check top-level keys
        self.assertIn("date", response.data)
        self.assertIn("weekly_threshold_percent", response.data)
        self.assertIn("monthly_threshold_percent", response.data)
        self.assertIn("tables", response.data)

    def test_growth_calculation(self):
        """Test that growth percentages are calculated correctly."""
        self._create_test_data()
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Find the growing_table in response
        growing_table = None
        for table in response.data["tables"]:
            if table["table_name"] == "growing_table":
                growing_table = table
                break

        self.assertIsNotNone(growing_table)
        # Weekly growth: (2MB - 1MB) / 1MB * 100 = 100%
        self.assertEqual(growing_table["weekly_growth_percent"], 100.0)
        # Monthly growth: (2MB - 0.5MB) / 0.5MB * 100 = 300%
        self.assertEqual(growing_table["monthly_growth_percent"], 300.0)

    def test_sorting_by_growth(self):
        """Test that tables are sorted by growth rate descending."""
        self._create_test_data()
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        tables = response.data["tables"]
        if len(tables) >= 2:
            # growing_table should be first due to higher growth
            self.assertEqual(tables[0]["table_name"], "growing_table")

    def test_table_name_filter(self):
        """Test filtering by table name."""
        self._create_test_data()
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, {"table_name": "growing"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should only return tables matching the filter
        for table in response.data["tables"]:
            self.assertIn("growing", table["table_name"].lower())

    @override_config(
        TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT=50,
        TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT=200,
    )
    def test_response_includes_alerts_when_threshold_exceeded(self):
        """Test that alerts are returned when growth exceeds thresholds."""
        self._create_test_data()
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("alerts", response.data)
        alerts = response.data["alerts"]
        # growing_table has 100% weekly growth (>50%) and 300% monthly growth (>200%)
        alert_entries = {(a["table_name"], a["period"]): a for a in alerts}
        self.assertIn(("growing_table", "weekly"), alert_entries)
        self.assertEqual(
            alert_entries[("growing_table", "weekly")]["growth_percent"], 100.0
        )
        self.assertEqual(alert_entries[("growing_table", "weekly")]["threshold"], 50)
        self.assertIn(("growing_table", "monthly"), alert_entries)
        self.assertEqual(
            alert_entries[("growing_table", "monthly")]["growth_percent"], 300.0
        )
        self.assertEqual(alert_entries[("growing_table", "monthly")]["threshold"], 200)

    @override_config(
        TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT=50,
        TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT=200,
    )
    def test_no_alerts_when_growth_below_threshold(self):
        """Test that no alerts are returned when growth is below thresholds."""
        # Create data with 20% weekly growth (below 50% threshold)
        DailyTableSizeHistory.objects.create(
            table_name="normal_table",
            date=self.today,
            total_size=1200000,
            data_size=900000,
            row_estimate=1200,
        )
        DailyTableSizeHistory.objects.create(
            table_name="normal_table",
            date=self.week_ago,
            total_size=1000000,
            data_size=750000,
            row_estimate=1000,
        )

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("alerts", response.data)
        self.assertEqual(len(response.data["alerts"]), 0)


class SampleTableSizesTaskTest(TestCase):
    """Tests for the sample_table_sizes Celery task."""

    @override_config(TABLE_GROWTH_MONITORING_ENABLED=False)
    def test_task_disabled_when_monitoring_disabled(self):
        """Test that task exits early when monitoring is disabled."""
        sample_table_sizes()
        # No entries should be created
        self.assertEqual(DailyTableSizeHistory.objects.count(), 0)

    @override_config(
        TABLE_GROWTH_MONITORING_ENABLED=True, TABLE_GROWTH_MIN_SIZE_BYTES=0
    )
    @patch("django.db.connection")
    def test_task_creates_entries(self, mock_connection):
        """Test that task creates history entries from database query."""
        # Mock the cursor and execute
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("test_table_1", 2000000, 1500000, 1000),
            ("test_table_2", 3000000, 2500000, 2000),
        ]
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor

        sample_table_sizes()

        # Check that entries were created
        self.assertEqual(DailyTableSizeHistory.objects.count(), 2)
        entry1 = DailyTableSizeHistory.objects.get(table_name="test_table_1")
        self.assertEqual(entry1.total_size, 2000000)
        self.assertEqual(entry1.data_size, 1500000)
        self.assertEqual(entry1.row_estimate, 1000)

    @override_config(
        TABLE_GROWTH_MONITORING_ENABLED=True,
        TABLE_GROWTH_MIN_SIZE_BYTES=0,
        TABLE_GROWTH_RETENTION_DAYS=7,
    )
    @patch("django.db.connection")
    def test_task_cleans_up_old_entries(self, mock_connection):
        """Test that task removes old entries beyond retention period."""
        # Create old entry
        old_date = timezone.now().date() - timedelta(days=30)
        DailyTableSizeHistory.objects.create(
            table_name="old_table",
            date=old_date,
            total_size=1000,
            data_size=500,
            row_estimate=10,
        )

        # Mock the cursor with empty result
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor

        sample_table_sizes()

        # Old entry should be deleted
        self.assertEqual(DailyTableSizeHistory.objects.count(), 0)


class CheckTableGrowthAlertsTaskTest(TestCase):
    """Tests for the check_table_growth_alerts Celery task."""

    def setUp(self):
        self.today = timezone.now().date()
        self.week_ago = self.today - timedelta(days=7)
        self.month_ago = self.today - timedelta(days=30)

    @override_config(TABLE_GROWTH_MONITORING_ENABLED=False)
    def test_task_disabled_when_monitoring_disabled(self):
        """Test that task exits early when monitoring is disabled."""
        check_table_growth_alerts()
        # Should complete without error

    @override_config(
        TABLE_GROWTH_MONITORING_ENABLED=True,
        TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT=50,
        TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT=200,
    )
    @patch("waldur_core.core.utils.broadcast_mail")
    def test_no_alert_when_no_data(self, mock_broadcast_mail):
        """Test that no alert is sent when there's no historical data."""
        check_table_growth_alerts()
        mock_broadcast_mail.assert_not_called()

    @override_config(
        TABLE_GROWTH_MONITORING_ENABLED=True,
        TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT=50,
        TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT=200,
    )
    @patch("waldur_core.core.utils.broadcast_mail")
    def test_no_alert_when_growth_below_threshold(self, mock_broadcast_mail):
        """Test that no alert is sent when growth is below threshold."""
        # Create data with 20% weekly growth (below 50% threshold)
        DailyTableSizeHistory.objects.create(
            table_name="normal_table",
            date=self.today,
            total_size=1200000,
            data_size=900000,
            row_estimate=1200,
        )
        DailyTableSizeHistory.objects.create(
            table_name="normal_table",
            date=self.week_ago,
            total_size=1000000,
            data_size=750000,
            row_estimate=1000,
        )

        check_table_growth_alerts()
        mock_broadcast_mail.assert_not_called()

    @override_config(
        TABLE_GROWTH_MONITORING_ENABLED=True,
        TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT=50,
        TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT=200,
    )
    @patch("waldur_core.core.utils.broadcast_mail")
    def test_alert_sent_when_weekly_threshold_exceeded(self, mock_broadcast_mail):
        """Test that alert is sent when weekly growth exceeds threshold."""
        # Create data with 100% weekly growth (above 50% threshold)
        DailyTableSizeHistory.objects.create(
            table_name="fast_growing_table",
            date=self.today,
            total_size=2000000,
            data_size=1500000,
            row_estimate=2000,
        )
        DailyTableSizeHistory.objects.create(
            table_name="fast_growing_table",
            date=self.week_ago,
            total_size=1000000,
            data_size=750000,
            row_estimate=1000,
        )

        # Create a staff user with email
        from waldur_core.core.models import User

        User.objects.create(
            username="staff_test",
            email="staff@test.com",
            is_staff=True,
            is_active=True,
            notifications_enabled=True,
        )

        check_table_growth_alerts()

        mock_broadcast_mail.assert_called_once()
        call_args = mock_broadcast_mail.call_args
        self.assertEqual(call_args[0][0], "core")
        self.assertEqual(call_args[0][1], "table_growth_alert")
        context = call_args[0][2]
        self.assertEqual(len(context["alerts"]), 1)
        self.assertEqual(context["alerts"][0]["table_name"], "fast_growing_table")
        self.assertEqual(context["alerts"][0]["period"], "weekly")
        self.assertEqual(context["alerts"][0]["growth_percent"], 100.0)

    @override_config(
        TABLE_GROWTH_MONITORING_ENABLED=True,
        TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT=50,
        TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT=200,
    )
    @patch("waldur_core.core.utils.broadcast_mail")
    def test_alert_sent_when_monthly_threshold_exceeded(self, mock_broadcast_mail):
        """Test that alert is sent when monthly growth exceeds threshold."""
        # Create data with 250% monthly growth (above 200% threshold)
        DailyTableSizeHistory.objects.create(
            table_name="monthly_growing_table",
            date=self.today,
            total_size=3500000,
            data_size=2500000,
            row_estimate=3500,
        )
        DailyTableSizeHistory.objects.create(
            table_name="monthly_growing_table",
            date=self.month_ago,
            total_size=1000000,
            data_size=750000,
            row_estimate=1000,
        )

        # Create a support user with email
        from waldur_core.core.models import User

        User.objects.create(
            username="support_test",
            email="support@test.com",
            is_support=True,
            is_active=True,
            notifications_enabled=True,
        )

        check_table_growth_alerts()

        mock_broadcast_mail.assert_called_once()
        call_args = mock_broadcast_mail.call_args
        context = call_args[0][2]
        self.assertEqual(len(context["alerts"]), 1)
        self.assertEqual(context["alerts"][0]["table_name"], "monthly_growing_table")
        self.assertEqual(context["alerts"][0]["period"], "monthly")
        self.assertEqual(context["alerts"][0]["growth_percent"], 250.0)


class TableGrowthTriggerAPITest(APITestCase):
    """Tests for the table growth trigger (POST) endpoint."""

    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.url = "/api/stats/table-growth/"

    def test_anonymous_user_cannot_trigger(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_trigger(self):
        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_support_user_cannot_trigger(self):
        support_user = self.fixture.user
        support_user.is_support = True
        support_user.save()
        self.client.force_authenticate(user=support_user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("waldur_core.core.tasks.sample_table_sizes.delay")
    def test_staff_user_can_trigger(self, mock_delay):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn("detail", response.data)
        mock_delay.assert_called_once()


class LegacyEndpointTest(APITestCase):
    """Tests that legacy endpoints still work."""

    def setUp(self):
        self.fixture = fixtures.UserFixture()

    def test_legacy_database_stats_endpoint(self):
        """Test that the old /api/database-stats/ endpoint still works."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get("/api/database-stats/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_legacy_celery_stats_endpoint(self):
        """Test that the old /api/celery-stats/ endpoint still works."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get("/api/celery-stats/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_new_stats_celery_endpoint(self):
        """Test that the new /api/stats/celery/ endpoint works."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get("/api/stats/celery/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_new_stats_database_endpoint(self):
        """Test that the new /api/stats/database/ endpoint works."""
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get("/api/stats/database/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
