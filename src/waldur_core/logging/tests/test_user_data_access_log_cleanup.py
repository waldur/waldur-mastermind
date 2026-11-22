from datetime import timedelta

from celery.schedules import crontab
from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from waldur_core.configuration.testing import override_config
from waldur_core.logging.models import UserDataAccessLog
from waldur_core.logging.tasks import cleanup_user_data_access_logs
from waldur_core.server.celeryconf import app
from waldur_core.structure.tests import factories as structure_factories

TASK_NAME = "waldur_core.logging.cleanup_user_data_access_logs"


class UserDataAccessLogCleanupScheduleTest(SimpleTestCase):
    def setUp(self):
        self.entry = settings.CELERY_BEAT_SCHEDULE["cleanup-user-data-access-logs"]

    def test_the_cleanup_runs_once_a_day(self):
        schedule = self.entry["schedule"]

        self.assertEqual(self.entry["task"], TASK_NAME)
        self.assertIsInstance(schedule, crontab)
        self.assertEqual(len(schedule.hour), 1)
        self.assertEqual(len(schedule.minute), 1)
        self.assertEqual(len(schedule.day_of_week), 7)
        self.assertEqual(len(schedule.day_of_month), 31)

    def test_the_scheduled_task_is_registered(self):
        app.loader.import_default_modules()

        self.assertIn(self.entry["task"], app.tasks)


class CleanupUserDataAccessLogsTaskTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)

    def create_log(self, age):
        log = UserDataAccessLog.objects.create(
            target_user=self.user,
            accessor=self.staff,
            accessor_type=UserDataAccessLog.AccessorType.STAFF,
            accessed_fields=["email"],
        )
        # timestamp is auto_now_add, so it can only be backdated afterwards
        UserDataAccessLog.objects.filter(pk=log.pk).update(
            timestamp=timezone.now() - age
        )
        return log

    @override_config(USER_DATA_ACCESS_LOG_RETENTION_DAYS=30)
    def test_logs_older_than_the_retention_period_are_deleted(self):
        expired = self.create_log(timedelta(days=31))
        kept = self.create_log(timedelta(days=29))

        result = cleanup_user_data_access_logs()

        self.assertEqual(result, {"deleted_count": 1, "retention_days": 30})
        self.assertFalse(UserDataAccessLog.objects.filter(pk=expired.pk).exists())
        self.assertTrue(UserDataAccessLog.objects.filter(pk=kept.pk).exists())

    @override_config(USER_DATA_ACCESS_LOG_RETENTION_DAYS=7)
    def test_the_retention_period_comes_from_the_setting(self):
        expired = self.create_log(timedelta(days=10))

        cleanup_user_data_access_logs()

        self.assertFalse(UserDataAccessLog.objects.filter(pk=expired.pk).exists())

    def test_zero_or_negative_retention_keeps_every_log(self):
        old = self.create_log(timedelta(days=400))
        recent = self.create_log(timedelta(hours=1))

        for retention_days in (0, -1):
            with self.subTest(retention_days=retention_days):
                with override_config(
                    USER_DATA_ACCESS_LOG_RETENTION_DAYS=retention_days
                ):
                    result = cleanup_user_data_access_logs()

                self.assertEqual(result["deleted_count"], 0)
                self.assertTrue(UserDataAccessLog.objects.filter(pk=old.pk).exists())
                self.assertTrue(UserDataAccessLog.objects.filter(pk=recent.pk).exists())
