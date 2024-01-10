from datetime import datetime, timedelta
from unittest import mock

import pytz
from croniter import croniter
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack_tenant import models, tasks

from ...tests import factories

TenantQuotas = openstack_models.Tenant.Quotas


class DeleteExpiredBackupsTaskTest(TestCase):
    def setUp(self):
        self.expired_backup1 = factories.BackupFactory(
            state=models.Backup.States.OK,
            kept_until=timezone.now() - timedelta(minutes=1),
        )
        self.expired_backup2 = factories.BackupFactory(
            state=models.Backup.States.OK,
            kept_until=timezone.now() - timedelta(minutes=10),
        )

    @mock.patch(
        "waldur_openstack.openstack_tenant.executors.BackupDeleteExecutor.execute"
    )
    def test_command_starts_backend_deletion(self, mocked_execute):
        tasks.DeleteExpiredBackups().run()
        mocked_execute.assert_has_calls(
            [
                mock.call(self.expired_backup1),
                mock.call(self.expired_backup2),
            ],
            any_order=True,
        )


class DeleteExpiredSnapshotsTaskTest(TestCase):
    def setUp(self):
        self.expired_snapshot1 = factories.SnapshotFactory(
            state=models.Snapshot.States.OK,
            kept_until=timezone.now() - timedelta(minutes=1),
        )
        self.expired_snapshot2 = factories.SnapshotFactory(
            state=models.Snapshot.States.OK,
            kept_until=timezone.now() - timedelta(minutes=10),
        )

    @mock.patch(
        "waldur_openstack.openstack_tenant.executors.SnapshotDeleteExecutor.execute"
    )
    def test_command_starts_snapshot_deletion(self, mocked_execute):
        tasks.DeleteExpiredSnapshots().run()
        mocked_execute.assert_has_calls(
            [
                mock.call(self.expired_snapshot1),
                mock.call(self.expired_snapshot2),
            ],
            any_order=True,
        )


class BackupScheduleTaskTest(TestCase):
    def setUp(self):
        self.disabled_schedule = factories.BackupScheduleFactory(is_active=False)

        self.instance = factories.InstanceFactory(
            state=models.Instance.States.OK,
        )
        self.overdue_schedule = factories.BackupScheduleFactory(
            instance=self.instance, timezone="Europe/Tallinn"
        )
        self.overdue_schedule.next_trigger_at = timezone.now() - timedelta(minutes=10)
        self.overdue_schedule.save()

        self.future_schedule = factories.BackupScheduleFactory(
            instance=self.instance, timezone="Europe/Tallinn"
        )
        self.future_schedule.next_trigger_at = timezone.now() + timedelta(minutes=2)
        self.future_schedule.save()

    def test_disabled_schedule_is_skipped(self):
        tasks.ScheduleBackups().run()
        self.assertEqual(self.disabled_schedule.backups.count(), 0)

    def test_backup_is_created_for_overdue_schedule(self):
        tasks.ScheduleBackups().run()
        self.assertEqual(self.overdue_schedule.backups.count(), 1)

    @mock.patch("waldur_openstack.openstack_tenant.handlers.log.event_logger")
    def test_if_quota_is_exceeded_backup_is_not_created_and_schedule_is_paused(
        self, event_logger
    ):
        schedule = self.overdue_schedule
        scope = self.instance.service_settings

        # Usage is equal to limit
        scope.set_quota_limit("snapshots", 2)
        scope.set_quota_usage("snapshots", 2)

        # Trigger task
        tasks.ScheduleBackups().run()
        schedule.refresh_from_db()

        # Backup is not created
        self.assertEqual(schedule.backups.count(), 0)

        # Schedule is deactivated
        self.assertFalse(schedule.is_active)

        # Error message is persisted in schedule
        self.assertTrue(schedule.error_message.startswith("Failed to schedule"))

        # Event is triggered for hooks
        event_type = event_logger.openstack_backup_schedule.warning.call_args[1][
            "event_type"
        ]
        self.assertEqual(event_type, "resource_backup_schedule_deactivated")

    def test_next_trigger_at_is_updated_for_overdue_schedule(self):
        # Arrange
        old_dt = self.overdue_schedule.next_trigger_at

        # Act
        tasks.ScheduleBackups().run()

        # Assert
        self.overdue_schedule.refresh_from_db()
        new_dt = self.overdue_schedule.next_trigger_at
        self.assertGreater(new_dt, old_dt)

    def test_next_trigger_at_is_updated_if_timezone_is_changed(self):
        # Arrange
        old_dt = self.future_schedule.next_trigger_at

        # Act
        self.future_schedule.timezone = "Asia/Tokyo"
        self.future_schedule.save()

        # Assert
        self.future_schedule.refresh_from_db()
        new_dt = self.future_schedule.next_trigger_at
        self.assertNotEqual(new_dt, old_dt)

    def test_duplicate_backups_are_not_created_for_two_consequent_immediate_calls(self):
        tasks.ScheduleBackups().run()
        self.assertEqual(self.overdue_schedule.backups.count(), 1)
        # timedelta is 0
        tasks.ScheduleBackups().run()
        self.assertEqual(self.overdue_schedule.backups.count(), 1)

    def test_two_backups_are_created_if_enough_time_has_passed(self):
        tasks.ScheduleBackups().run()
        self.assertEqual(self.overdue_schedule.backups.count(), 1)

        self._trigger_next_backup(timezone.now())
        self.assertEqual(self.overdue_schedule.backups.count(), 2)

    def test_future_schedule_is_skipped(self):
        tasks.ScheduleBackups().run()
        self.assertEqual(self.future_schedule.backups.count(), 0)

    def test_command_does_not_create_more_backups_than_maximal_number_of_resources(
        self,
    ):
        maximum_number = 3
        self.overdue_schedule.maximal_number_of_resources = maximum_number
        self.overdue_schedule.save()
        tasks.ScheduleBackups().run()

        self.assertEqual(self.overdue_schedule.backups.count(), 1)
        base_time = self._trigger_next_backup(timezone.now())
        self.assertEqual(self.overdue_schedule.backups.count(), 2)
        base_time = self._trigger_next_backup(base_time)
        self.assertEqual(self.overdue_schedule.backups.count(), 3)
        self._trigger_next_backup(base_time)

        self.assertEqual(self.overdue_schedule.backups.count(), maximum_number)

    def test_command_creates_backups_up_to_maximal_number_if_limit_is_updated(self):
        self.overdue_schedule.maximal_number_of_resources = 2
        self.overdue_schedule.save()
        tasks.ScheduleBackups().run()

        self.assertEqual(self.overdue_schedule.backups.count(), 1)
        base_time = self._trigger_next_backup(timezone.now())
        self.assertEqual(self.overdue_schedule.backups.count(), 2)
        base_time = self._trigger_next_backup(base_time)
        self.assertEqual(self.overdue_schedule.backups.count(), 2)

        self.overdue_schedule.maximal_number_of_resources = 3
        self.overdue_schedule.save()
        self._trigger_next_backup(base_time)
        self.assertEqual(self.overdue_schedule.backups.count(), 3)

    def test_if_backup_amount_exceeds_allowed_limit_deletion_is_scheduled(self):
        now = datetime.now()
        todays_backup = factories.BackupFactory(instance=self.instance, kept_until=now)
        older_backup = factories.BackupFactory(
            instance=self.instance, kept_until=now - timedelta(minutes=30)
        )
        oldest_backup = factories.BackupFactory(
            instance=self.instance, kept_until=now - timedelta(minutes=50)
        )
        self.overdue_schedule.backups.add(*[todays_backup, older_backup, oldest_backup])
        self.overdue_schedule.maximal_number_of_resources = 1
        self.overdue_schedule.save()
        tasks.ScheduleBackups().run()

        older_backup.refresh_from_db()
        oldest_backup.refresh_from_db()
        self.assertEqual(models.Backup.States.DELETION_SCHEDULED, older_backup.state)
        self.assertEqual(models.Backup.States.DELETION_SCHEDULED, oldest_backup.state)

        tasks.ScheduleBackups().run()
        self.assertTrue(models.Backup.objects.filter(id=todays_backup.id).exists())
        self.assertEqual(self.overdue_schedule.backups.count(), 3)

    def test_if_backup_amount_equals_allowed_limit_deletion_is_scheduled_for_oldest_backup(
        self,
    ):
        now = datetime.now()
        backup1 = factories.BackupFactory(
            instance=self.instance, kept_until=None, created=now - timedelta(days=3)
        )
        backup2 = factories.BackupFactory(
            instance=self.instance, kept_until=None, created=now - timedelta(days=2)
        )
        backup3 = factories.BackupFactory(
            instance=self.instance, kept_until=None, created=now - timedelta(days=1)
        )

        self.overdue_schedule.backups.add(*[backup1, backup2, backup3])
        self.overdue_schedule.maximal_number_of_resources = 3
        self.overdue_schedule.save()
        tasks.ScheduleBackups().run()

        backup1.refresh_from_db()
        backup2.refresh_from_db()
        backup3.refresh_from_db()
        self.assertEqual(models.Backup.States.DELETION_SCHEDULED, backup1.state)
        self.assertNotEqual(models.Backup.States.DELETION_SCHEDULED, backup2.state)
        self.assertNotEqual(models.Backup.States.DELETION_SCHEDULED, backup3.state)

    @mock.patch(
        "waldur_openstack.openstack_tenant.executors.BackupDeleteExecutor.execute"
    )
    def test_if_exceeding_backups_are_already_deleting_extra_deletion_is_not_scheduled(
        self, mocked_executor
    ):
        backup1 = factories.BackupFactory(
            instance=self.instance, state=models.Backup.States.DELETION_SCHEDULED
        )
        backup2 = factories.BackupFactory(instance=self.instance)
        backup3 = factories.BackupFactory(instance=self.instance)

        self.overdue_schedule.backups.add(*[backup1, backup2, backup3])
        self.overdue_schedule.maximal_number_of_resources = 3
        self.overdue_schedule.save()
        tasks.ScheduleBackups().run()

        self.assertEqual(0, mocked_executor.call_count)

    def _trigger_next_backup(self, base_dt: datetime):
        tz = pytz.timezone(self.overdue_schedule.timezone)
        dt = base_dt.astimezone(tz)
        next_trigger_at = croniter(self.overdue_schedule.schedule, dt).get_next(
            datetime
        )
        mocked_now = next_trigger_at + timedelta(seconds=5)
        with freeze_time(mocked_now):
            tasks.ScheduleBackups().run()

        return mocked_now


class SnapshotScheduleTaskTest(TestCase):
    def test_command_does_not_create_snapshots_created_for_not_active_schedules(self):
        self.not_active_schedule = factories.SnapshotScheduleFactory(is_active=False)

        tasks.ScheduleSnapshots().run()

        self.assertEqual(self.not_active_schedule.snapshots.count(), 0)

    def test_command_create_one_snapshot_for_schedule_with_next_trigger_in_past(self):
        self.schedule_for_execution = factories.SnapshotScheduleFactory()
        self.schedule_for_execution.next_trigger_at = timezone.now() - timedelta(
            minutes=10
        )
        self.schedule_for_execution.save()

        tasks.ScheduleSnapshots().run()

        self.assertEqual(self.schedule_for_execution.snapshots.count(), 1)

    def test_command_does_not_create_snapshots_created_for_schedule_with_next_trigger_in_future(
        self,
    ):
        self.future_schedule = factories.SnapshotScheduleFactory()
        self.future_schedule.next_trigger_at = timezone.now() + timedelta(minutes=2)
        self.future_schedule.save()

        tasks.ScheduleSnapshots().run()

        self.assertEqual(self.future_schedule.snapshots.count(), 0)

    @mock.patch("waldur_openstack.openstack_tenant.handlers.log.event_logger")
    def test_if_quota_is_exceeded_snapshot_is_not_created_and_schedule_is_paused(
        self, event_logger
    ):
        schedule = factories.SnapshotScheduleFactory()
        schedule.next_trigger_at = timezone.now() - timedelta(minutes=10)
        schedule.save()
        scope = schedule.source_volume.service_settings

        # Usage is equal to limit
        scope.set_quota_limit("snapshots", 2)
        scope.set_quota_usage("snapshots", 2)

        # Trigger task
        tasks.ScheduleSnapshots().run()
        schedule.refresh_from_db()

        # Snapshot is not created
        self.assertEqual(schedule.snapshots.count(), 0)

        # Schedule is deactivated
        self.assertFalse(schedule.is_active)

        # Error message is persisted in schedule
        self.assertTrue(schedule.error_message.startswith("Failed to schedule"))

        # Event is triggered for hooks
        event_type = event_logger.openstack_snapshot_schedule.warning.call_args[1][
            "event_type"
        ]
        self.assertEqual(event_type, "resource_snapshot_schedule_deactivated")
