from datetime import timedelta
from unittest import mock

from cinderclient import exceptions as cinder_exceptions
from django.test import TestCase
from django.utils import timezone

from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_openstack import executors, models, tasks
from waldur_openstack.exceptions import OpenStackBackendError

from . import factories, fixtures

TenantQuotas = models.Tenant.Quotas


class DeleteExpiredBackupsTaskTest(TestCase):
    def setUp(self):
        self.expired_backup1 = factories.BackupFactory(
            state=CoreStates.OK,
            kept_until=timezone.now() - timedelta(minutes=1),
        )
        self.expired_backup2 = factories.BackupFactory(
            state=CoreStates.OK,
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
            state=CoreStates.OK,
            kept_until=timezone.now() - timedelta(minutes=1),
        )
        self.expired_snapshot2 = factories.SnapshotFactory(
            state=CoreStates.OK,
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


class PollBackupSnapshotsDeletedTaskTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.backup = self.fixture.backup
        self.mocked_cinder = mock.patch("cinderclient.v3.client.Client").start()()
        fixtures.mock_session()
        self.snapshot_statuses = {}
        self.mocked_cinder.volume_snapshots.get.side_effect = self._get_snapshot
        self.task = tasks.PollBackupSnapshotsDeletedTask()
        self.task.retry = mock.Mock(side_effect=Exception("Task retried"))

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def _get_snapshot(self, backend_id):
        status = self.snapshot_statuses[backend_id]
        if status is None:
            raise cinder_exceptions.NotFound(code=404)
        return mock.Mock(id=backend_id, status=status)

    def _add_snapshot(self, backend_id, status):
        snapshot = factories.SnapshotFactory(
            tenant=self.tenant,
            project=self.fixture.project,
            backend_id=backend_id,
            state=CoreStates.DELETING,
        )
        snapshot.increase_backend_quotas_usage()
        self.backup.snapshots.add(snapshot)
        self.snapshot_statuses[backend_id] = status
        return snapshot

    def test_deleted_snapshots_are_removed_and_release_quotas(self):
        self._add_snapshot("a", None)
        self._add_snapshot("b", None)

        self.task.execute(self.backup)

        self.assertFalse(self.backup.snapshots.exists())
        self.assertEqual(self.tenant.get_quota_usage("snapshots"), 0)

    def test_refused_snapshot_does_not_stop_polling_of_the_others(self):
        refused = self._add_snapshot("a", "available")
        self._add_snapshot("b", None)

        with self.assertRaisesRegex(OpenStackBackendError, "dependent volumes"):
            self.task.execute(self.backup)

        self.assertEqual(list(self.backup.snapshots.all()), [refused])
        refused.refresh_from_db()
        self.assertEqual(refused.state, CoreStates.ERRED)
        self.assertIn("dependent volumes", refused.error_message)
        self.assertEqual(self.tenant.get_quota_usage("snapshots"), 1)

    def test_snapshot_still_deleting_is_polled_again(self):
        deleting = self._add_snapshot("a", "deleting")
        self._add_snapshot("b", None)

        with self.assertRaisesRegex(Exception, "Task retried"):
            self.task.execute(self.backup)

        self.assertEqual(list(self.backup.snapshots.all()), [deleting])
        self.assertEqual(self.tenant.get_quota_usage("snapshots"), 1)

    def test_throttled_poll_is_retried_without_failing_the_snapshot(self):
        snapshot = self._add_snapshot("a", "deleting")
        self.mocked_cinder.volume_snapshots.get.side_effect = (
            cinder_exceptions.OverLimit(code=413)
        )

        with self.assertRaisesRegex(Exception, "Task retried"):
            self.task.execute(self.backup)

        snapshot.refresh_from_db()
        self.assertEqual(snapshot.state, CoreStates.DELETING)
        self.assertEqual(
            self.task.retry.call_args.kwargs["countdown"],
            self.task.default_retry_delay,
        )

    def test_backup_deletion_polls_all_snapshots_in_one_task(self):
        self._add_snapshot("a", None)
        self._add_snapshot("b", None)

        signature = executors.BackupDeleteExecutor.get_task_signature(
            self.backup, core_utils.serialize_instance(self.backup)
        )

        task_names = [task.task for task in signature.tasks]
        self.assertEqual(task_names.count(tasks.PollBackupSnapshotsDeletedTask.name), 1)
        self.assertNotIn(core_tasks.PollBackendCheckTask.name, task_names)
        self.assertNotIn(core_tasks.DeletionTask.name, task_names)
