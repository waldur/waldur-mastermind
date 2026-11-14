"""
Regression tests for OpenPortal code paths that called attributes which do
not exist: ``ensure_config_loaded`` on the settings store instead of the
OpenPortal config, ``openportal.Status.PENDING`` and ``Job(id=, data=)`` in
``sync_board``, and ``_to_user_identifier`` on the remote client.

Only the boundary is stubbed (whether OpenPortal is configured, the portal
name, the board and the command runner); the functions under test run for
real.
"""

import uuid
from unittest import mock

import openportal
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_openportal import config as openportal_config
from waldur_openportal import models, tasks, utils
from waldur_openportal.remoteclient import RemoteOpenPortalClient


class LocalProjectIdentifierTest(TestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        models.ProjectInfo.objects.create(project=self.project, shortname="xshort")

    def test_openportal_config_is_consulted(self):
        self.assertIs(utils.config, openportal_config)

    @mock.patch("openportal.get_portal", return_value="awards")
    @mock.patch("waldur_openportal.config.ensure_config_loaded", return_value=True)
    def test_identifier_is_project_shortname_at_this_portal(self, *_):
        self.assertEqual(
            str(utils.get_local_project_identifier(self.project)), "xshort.awards"
        )

    @mock.patch("waldur_openportal.config.ensure_config_loaded", return_value=False)
    def test_unconfigured_portal_raises_runtime_error(self, _):
        with self.assertRaises(RuntimeError):
            utils.get_local_project_identifier(self.project)


class RefreshRemoteProjectTest(TestCase):
    @mock.patch("waldur_openportal.config.ensure_config_loaded", return_value=False)
    def test_unconfigured_portal_returns_none(self, _):
        remote_project = mock.Mock()
        remote_project.current_project = structure_factories.ProjectFactory()
        self.assertIsNone(utils.refresh_remote_project(remote_project))


@mock.patch("waldur_openportal.tasks.run_job.delay")
@mock.patch("waldur_openportal.tasks.openportal.fetch_jobs")
@mock.patch("waldur_openportal.tasks.config.ensure_config_loaded", return_value=True)
class SyncBoardTest(TestCase):
    def board_job(self, state):
        job = mock.Mock()
        job.id = uuid.uuid4()
        job.state = state
        job.to_json.return_value = '{"job": "data"}'
        return job

    def test_pending_job_is_stored_and_dispatched(self, _, fetch_jobs, delay):
        job = self.board_job(openportal.Status.pending())
        fetch_jobs.return_value = [job]

        tasks.sync_board()

        stored = models.Job.objects.get(job_id=str(job.id))
        self.assertEqual(stored.job_data, '{"job": "data"}')
        self.assertEqual(stored.state, models.Job.State.PENDING)
        delay.assert_called_once()

    def test_job_polled_twice_is_stored_once(self, _, fetch_jobs, delay):
        job = self.board_job(openportal.Status.pending())
        fetch_jobs.return_value = [job]

        tasks.sync_board()
        models.Job.objects.filter(job_id=str(job.id)).update(
            state=models.Job.State.RUNNING
        )
        tasks.sync_board()

        self.assertEqual(models.Job.objects.filter(job_id=str(job.id)).count(), 1)
        delay.assert_called_once()

    def test_job_that_is_not_pending_is_skipped(self, _, fetch_jobs, delay):
        fetch_jobs.return_value = [self.board_job(openportal.Status.running())]

        tasks.sync_board()

        self.assertFalse(models.Job.objects.exists())
        delay.assert_not_called()


class RemoteClientDeleteUserTest(TestCase):
    @mock.patch("waldur_openportal.config.ensure_config_loaded", return_value=True)
    def test_delete_user_sends_remove_user(self, _):
        client = RemoteOpenPortalClient("remote.brics", "default")
        with mock.patch.object(client, "run") as run:
            client.delete_user("alice.proj.portal")

        run.assert_called_once_with("remote.brics remove_user alice.proj.portal")
