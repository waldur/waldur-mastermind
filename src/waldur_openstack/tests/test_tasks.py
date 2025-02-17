from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from waldur_openstack import models, tasks

from . import factories

TenantQuotas = models.Tenant.Quotas


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

    @mock.patch("waldur_openstack.executors.BackupDeleteExecutor.execute")
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

    @mock.patch("waldur_openstack.executors.SnapshotDeleteExecutor.execute")
    def test_command_starts_snapshot_deletion(self, mocked_execute):
        tasks.DeleteExpiredSnapshots().run()
        mocked_execute.assert_has_calls(
            [
                mock.call(self.expired_snapshot1),
                mock.call(self.expired_snapshot2),
            ],
            any_order=True,
        )
