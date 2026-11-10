from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
from kombu import Connection, Queue
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.demo_presets import jobs, tasks
from waldur_mastermind.marketplace.demo_presets.manifest import DemoPresetManager
from waldur_mastermind.marketplace.tests.test_demo_presets import (
    demo_presets_settings,
)

States = models.DemoPresetLoad.States
LOAD_URL = "/api/marketplace-demo-presets/load/minimal_quickstart/"
STATUS_URL = "/api/demo-preset-status/"


def make_load(state=States.RUNNING, preset="minimal_quickstart", **kwargs):
    if state == States.RUNNING:
        kwargs.setdefault("started_at", timezone.now())
    return models.DemoPresetLoad.objects.create(preset=preset, state=state, **kwargs)


@demo_presets_settings()
@mock.patch.object(tasks.load_demo_preset, "delay")
class LoadRequestTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)
        purge = mock.patch.object(jobs, "purge_task_queues", return_value=7)
        self.purge = purge.start()
        self.addCleanup(purge.stop)

    def test_cleanup_load_drops_queued_tasks_before_queueing_itself(self, delay):
        delay.side_effect = lambda uuid: self.assertTrue(self.purge.called)

        response = self.client.post(LOAD_URL)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.purge.assert_called_once_with()
        delay.assert_called_once()
        job = models.DemoPresetLoad.objects.get()
        self.assertIn("Dropped 7 queued task(s)", job.output)

    def test_load_without_cleanup_keeps_queued_tasks(self, delay):
        response = self.client.post(LOAD_URL, {"cleanup_first": False})

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.purge.assert_not_called()
        delay.assert_called_once()

    def test_unreachable_broker_fails_the_load_at_once(self, delay):
        self.purge.side_effect = ConnectionRefusedError("broker down")

        response = self.client.post(LOAD_URL)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertNotIn("demo_reset", response.json())
        delay.assert_not_called()
        job = models.DemoPresetLoad.objects.get()
        self.assertEqual(job.state, States.ERRED)
        self.assertIn("broker down", job.error_message)
        # The API is not left paused behind a load that will never run.
        self.assertEqual(self.client.get("/api/customers/").status_code, 200)

    def test_failure_to_queue_the_task_fails_the_load(self, delay):
        delay.side_effect = ConnectionRefusedError("broker down")

        response = self.client.post(LOAD_URL)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(models.DemoPresetLoad.objects.get().state, States.ERRED)

    def test_load_is_queued_and_answered_with_202(self, delay):
        response = self.client.post(LOAD_URL, {"skip_roles": True})

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        job = models.DemoPresetLoad.objects.get(uuid=response.data["uuid"])
        self.assertEqual(job.state, States.PENDING)
        self.assertEqual(job.requested_by, self.staff.username)
        self.assertEqual(
            job.options,
            {"cleanup_first": True, "skip_users": False, "skip_roles": True},
        )
        delay.assert_called_once_with(job.uuid.hex)

    def test_second_load_while_one_runs_is_refused(self, delay):
        make_load()

        # Another request that passed the middleware before the first load
        # was created: the view still refuses it.
        with mock.patch(
            "waldur_mastermind.marketplace.demo_presets.middleware.jobs"
        ) as middleware_jobs:
            middleware_jobs.active_load.return_value = None
            response = self.client.post(LOAD_URL)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(models.DemoPresetLoad.objects.count(), 1)
        delay.assert_not_called()

    def test_finished_load_does_not_block_a_new_one(self, delay):
        make_load(state=States.ERRED)

        response = self.client.post(LOAD_URL)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_abandoned_load_is_erred_and_no_longer_blocks(self, delay):
        stale = make_load(
            started_at=timezone.now() - jobs.STALE_AFTER - timedelta(minutes=1)
        )

        response = self.client.post(LOAD_URL)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        stale.refresh_from_db()
        self.assertEqual(stale.state, States.ERRED)
        self.assertTrue(stale.error_message)

    def test_dry_run_is_answered_directly(self, delay):
        response = self.client.post(LOAD_URL, {"dry_run": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])
        self.assertFalse(models.DemoPresetLoad.objects.exists())
        delay.assert_not_called()


@demo_presets_settings()
class ApiPausedWhileLoadingTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    def test_api_answers_503_while_a_load_runs(self):
        make_load(preset="hpc_ai_platform")

        response = self.client.get("/api/customers/")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(
            response.json()["demo_reset"], True, "Homeport tells a reset by this flag"
        )
        self.assertEqual(response.json()["preset"], "hpc_ai_platform")
        self.assertIn("Retry-After", response)

    def test_load_request_is_paused_too(self):
        make_load()

        self.assertEqual(self.client.post(LOAD_URL).status_code, 503)

    def test_pending_load_pauses_too(self):
        make_load(state=States.PENDING)

        response = self.client.get("/api/customers/")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_status_configuration_and_login_keep_answering(self):
        make_load()

        self.assertEqual(self.client.get(STATUS_URL).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.get("/api/configuration/").status_code, status.HTTP_200_OK
        )
        login = self.client.post(
            "/api-auth/password/", {"username": "nobody", "password": "wrong"}
        )
        self.assertNotEqual(login.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_api_answers_once_the_load_is_done(self):
        make_load(state=States.DONE)

        self.assertEqual(self.client.get("/api/customers/").status_code, 200)

    def test_abandoned_load_does_not_pause_the_api(self):
        make_load(started_at=timezone.now() - jobs.STALE_AFTER - timedelta(minutes=1))

        self.assertEqual(self.client.get("/api/customers/").status_code, 200)


class GateOffTest(test.APITestCase):
    @demo_presets_settings(enabled=False)
    def test_middleware_does_not_look_for_loads(self):
        make_load()
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

        with mock.patch.object(jobs, "active_load") as active_load:
            response = self.client.get("/api/customers/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        active_load.assert_not_called()

    @demo_presets_settings(enabled=False)
    def test_status_is_not_found(self):
        self.assertEqual(
            self.client.get(STATUS_URL).status_code, status.HTTP_404_NOT_FOUND
        )


@demo_presets_settings()
class StatusTest(test.APITestCase):
    def test_anonymous_sees_status_without_a_load(self):
        response = self.client.get(STATUS_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"loading": False, "load": None, "users": []})

    def test_a_token_the_load_deleted_does_not_get_in_the_way(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token deleted-by-the-load")

        self.assertEqual(self.client.get(STATUS_URL).status_code, status.HTTP_200_OK)

    def test_running_load_is_reported_without_users(self):
        job = make_load()

        data = self.client.get(STATUS_URL).data

        self.assertTrue(data["loading"])
        self.assertEqual(data["load"]["uuid"], job.uuid.hex)
        self.assertEqual(data["load"]["state"], States.RUNNING)
        self.assertEqual(data["users"], [])

    def test_done_load_lists_users_without_staff_or_support_role(self):
        make_load(state=States.DONE, preset="hpc_ai_platform")
        preset_users = DemoPresetManager.get_preset_users("hpc_ai_platform")
        self.assertTrue(any(u["is_staff"] for u in preset_users))
        self.assertTrue(any(u["is_support"] for u in preset_users))

        data = self.client.get(STATUS_URL).data

        self.assertFalse(data["loading"])
        self.assertEqual(
            {u["username"] for u in data["users"]},
            {
                u["username"]
                for u in preset_users
                if not u["is_staff"] and not u["is_support"]
            },
        )
        self.assertTrue(data["users"])
        self.assertFalse(any(u["is_staff"] or u["is_support"] for u in data["users"]))

    def test_erred_load_lists_no_users(self):
        make_load(state=States.ERRED, error_message="boom")

        data = self.client.get(STATUS_URL).data

        self.assertEqual(data["load"]["error_message"], "boom")
        self.assertEqual(data["users"], [])

    def test_load_without_users_lists_none(self):
        make_load(state=States.DONE, options={"skip_users": True})

        self.assertEqual(self.client.get(STATUS_URL).data["users"], [])

    def test_abandoned_load_is_reported_erred(self):
        with freeze_time(timezone.now() - jobs.STALE_AFTER - timedelta(minutes=1)):
            make_load(state=States.PENDING)

        data = self.client.get(STATUS_URL).data

        self.assertFalse(data["loading"])
        self.assertEqual(data["load"]["state"], States.ERRED)


@mock.patch.object(DemoPresetManager, "load_preset")
class LoadTaskTest(test.APITestCase):
    def test_success_is_recorded(self, load_preset):
        load_preset.return_value = {"success": True, "message": "ok", "output": "log"}
        job = make_load(
            state=States.PENDING, options={"skip_roles": True}, output="Dropped 2.\n"
        )

        tasks.load_demo_preset(job.uuid.hex)

        load_preset.assert_called_once_with(name="minimal_quickstart", skip_roles=True)
        job.refresh_from_db()
        self.assertEqual(job.state, States.DONE)
        self.assertEqual(job.output, "Dropped 2.\nlog")
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)

    def test_failed_load_is_erred_with_its_message(self, load_preset):
        load_preset.return_value = {
            "success": False,
            "message": "Cleanup failed",
            "output": "log",
        }
        job = make_load(state=States.PENDING)

        tasks.load_demo_preset(job.uuid.hex)

        job.refresh_from_db()
        self.assertEqual(job.state, States.ERRED)
        self.assertEqual(job.error_message, "Cleanup failed")

    def test_exception_is_erred_and_releases_the_api(self, load_preset):
        load_preset.side_effect = RuntimeError("worker crashed")
        job = make_load(state=States.PENDING)

        tasks.load_demo_preset(job.uuid.hex)

        job.refresh_from_db()
        self.assertEqual(job.state, States.ERRED)
        self.assertEqual(job.error_message, "worker crashed")
        self.assertIsNone(jobs.active_load())

    def test_load_that_is_not_pending_is_not_run_again(self, load_preset):
        job = make_load(state=States.RUNNING)

        tasks.load_demo_preset(job.uuid.hex)

        load_preset.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, States.RUNNING)


# The in-memory broker keeps one state per process, which earlier tests fill
# with the tasks they queue, so these tests use queue names of their own.
@override_settings(
    TASKS_QUEUE="purge-test-tasks",
    HEAVY_QUEUE="purge-test-heavy",
    BACKGROUND_QUEUE="purge-test-background",
)
class PurgeTaskQueuesTest(test.APITestCase):
    def setUp(self):
        # An in-memory broker of its own: Celery reads CELERY_BROKER_URL from
        # the environment, which may point at a developer's real RabbitMQ.
        connect = mock.patch.object(
            jobs.current_app,
            "connection_for_write",
            side_effect=lambda: Connection("memory://"),
        )
        connect.start()
        self.addCleanup(connect.stop)

    def _queue_task(self, name):
        with Connection("memory://") as connection:
            # Declared as a worker does on start; the in-memory broker drops
            # messages routed to a queue nobody declared.
            bound = Queue(name, routing_key=name)(connection.default_channel)
            bound.declare()
            connection.Producer().publish(
                {"task": "waldur.stale_task"},
                exchange=bound.exchange,
                routing_key=bound.routing_key,
            )

    def test_drops_tasks_queued_for_the_workers(self):
        self._queue_task(settings.TASKS_QUEUE)
        self._queue_task(settings.BACKGROUND_QUEUE)

        self.assertEqual(jobs.purge_task_queues(), 2)
        self.assertEqual(jobs.purge_task_queues(), 0)

    def test_queue_never_declared_counts_as_empty(self):
        with override_settings(TASKS_QUEUE="purge-test-never-declared"):
            self.assertEqual(jobs.purge_task_queues(), 0)
