from celery.schedules import crontab
from django.test import SimpleTestCase

from waldur_core.core.management.commands.print_scheduled_jobs import format_schedule


class FormatScheduleTest(SimpleTestCase):
    def test_named_schedules(self):
        self.assertEqual(format_schedule(crontab(minute=0)), "Hourly (at minute 0)")
        self.assertEqual(
            format_schedule(crontab(minute=0, hour=0)), "Daily (at midnight)"
        )
        self.assertEqual(
            format_schedule(crontab(minute=0, hour=0, day_of_week=0)),
            "Weekly (every Sunday at midnight)",
        )
        self.assertEqual(
            format_schedule(crontab(minute=0, hour=0, day_of_month=1)),
            "Monthly (1st day of month at midnight)",
        )

    def test_every_ten_minutes_is_not_hourly(self):
        # "*/10 * * * *" contains "0 * * * *".
        self.assertEqual(
            format_schedule(crontab(minute="*/10")),
            "Cron: `*/10 * * * * (m/h/dM/MY/d)`",
        )

    def test_half_past_midnight_is_not_midnight(self):
        self.assertEqual(
            format_schedule(crontab(minute=30, hour=0)),
            "Cron: `30 0 * * * (m/h/dM/MY/d)`",
        )
