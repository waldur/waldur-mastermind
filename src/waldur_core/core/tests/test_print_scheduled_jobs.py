import importlib

from celery import current_app
from celery.schedules import crontab
from django.test import SimpleTestCase

from waldur_core.core.management.commands.print_scheduled_jobs import format_schedule
from waldur_core.server import celeryconf


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


class CeleryCurrentAppIdentityTest(SimpleTestCase):
    def test_aliased_server_import_does_not_replace_current_app(self):
        # Pytest imports test modules under ``src.waldur_core.server.*``;
        # ``waldur_core.server.__init__`` must reuse the canonical celeryconf
        # module instead of creating a second Celery app that hijacks current_app.
        aliased = importlib.import_module("src.waldur_core.server")
        self.assertIs(aliased.celery_app, celeryconf.app)
        self.assertIs(current_app._get_current_object(), celeryconf.app)
