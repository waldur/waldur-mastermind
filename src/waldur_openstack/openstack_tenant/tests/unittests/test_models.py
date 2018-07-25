from __future__ import unicode_literals

from croniter import croniter
import datetime
import freezegun

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from .. import factories, fixtures
from ... import models


class InstanceTest(TestCase):
    def test_instance_size_is_sum_of_volumes_size(self):
        fixture = fixtures.OpenStackTenantFixture()
        expected_size = sum(fixture.instance.volumes.all().values_list('size', flat=True))
        self.assertEqual(fixture.instance.size, expected_size)


class BackupScheduleTest(TestCase):
    def setUp(self):
        self.openstack_tenant_fixture = fixtures.OpenStackTenantFixture()
        self.instance = self.openstack_tenant_fixture.instance

    def test_update_next_trigger_at(self):
        now = timezone.now()
        schedule = factories.BackupScheduleFactory()
        schedule.schedule = '*/10 * * * *'
        schedule.update_next_trigger_at()
        self.assertTrue(schedule.next_trigger_at)
        self.assertGreater(schedule.next_trigger_at, now)

    def test_update_next_trigger_at_with_provided_timezone(self):
        schedule = factories.BackupScheduleFactory(timezone='Europe/London')
        schedule.update_next_trigger_at()

        # next_trigger_at timezone and schedule's timezone must be equal.
        self.assertEqual(schedule.timezone, schedule.next_trigger_at.tzinfo.zone)

    def test_update_next_trigger_at_with_default_timezone(self):
        schedule = factories.BackupScheduleFactory()
        schedule.update_next_trigger_at()

        # If timezone is not provided, default timezone must be set.
        self.assertEqual(settings.TIME_ZONE, schedule.timezone)

    def test_save(self):
        # new schedule
        schedule = factories.BackupScheduleFactory(next_trigger_at=None)
        self.assertGreater(schedule.next_trigger_at, timezone.now())

        # schedule become active
        schedule.is_active = False
        schedule.next_trigger_at = None
        schedule.save()
        schedule.is_active = True
        schedule.save()
        self.assertGreater(schedule.next_trigger_at, timezone.now())

        # schedule was changed
        schedule.next_trigger_at = None
        schedule.schedule = '*/10 * * * *'
        schedule.save()
        schedule = models.BackupSchedule.objects.get(id=schedule.id)
        self.assertGreater(schedule.next_trigger_at, timezone.now())

    def test_weekly_backup_schedule_next_trigger_at_is_correct(self):
        schedule = factories.BackupScheduleFactory(schedule='0 2 * * 4')

        cron = croniter('0 2 * * 4', timezone.now())
        next_backup = schedule.next_trigger_at
        self.assertEqual(next_backup, cron.get_next(datetime.datetime))
        self.assertEqual(next_backup.weekday(), 3, 'Must be Thursday')

        for k, v in {'hour': 2, 'minute': 0, 'second': 0}.items():
            self.assertEqual(getattr(next_backup, k), v, 'Must be 2:00am')

    def test_daily_backup_schedule_next_trigger_at_is_correct(self):
        schedule = '0 2 * * *'

        today = timezone.now()
        expected = croniter(schedule, today).get_next(datetime.datetime)

        with freezegun.freeze_time(today):
            self.assertEqual(expected, factories.BackupScheduleFactory(schedule=schedule).next_trigger_at)


class SnapshotScheduleTest(TestCase):

    def test_weekly_snapshot_schedule_next_trigger_at_is_correct(self):
        schedule = factories.SnapshotScheduleFactory(schedule='0 2 * * 4')

        cron = croniter('0 2 * * 4', timezone.now())
        next_snapshot = schedule.next_trigger_at
        self.assertEqual(next_snapshot, cron.get_next(datetime.datetime))
        self.assertEqual(next_snapshot.weekday(), 3, 'Must be Thursday')

        for k, v in {'hour': 2, 'minute': 0, 'second': 0}.items():
            self.assertEqual(getattr(next_snapshot, k), v, 'Must be 2:00am')

    def test_daily_snapshot_schedule_next_trigger_at_is_correct(self):
        schedule = '0 2 * * *'

        today = timezone.now()
        expected = croniter(schedule, today).get_next(datetime.datetime)

        with freezegun.freeze_time(today):
            self.assertEqual(expected, factories.SnapshotScheduleFactory(schedule=schedule).next_trigger_at)
