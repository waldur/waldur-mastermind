import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.models import QuerySet
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.metadata import WaldurCore
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.logging import tasks
from waldur_core.logging.models import Event, Feed
from waldur_core.logging.tests import factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

CONTAINER_SETTINGS = (
    Path(__file__).resolve().parents[4] / "docker/rootfs/etc/waldur/settings.py"
)

PROBE = textwrap.dedent(
    """
    import runpy, sys
    print(runpy.run_path(sys.argv[1])["WALDUR_CORE"]["EVENT_RETENTION_DAYS"])
    """
)


class EventRetentionTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.staff = self.fixture.staff
        self.admin = self.fixture.admin
        # The fixture writes events of its own (creation, role grants); clear
        # them so each test counts only the events it creates.
        Event.objects.all().delete()

    def make_events(self, count, age_days, scope=None):
        events = [factories.EventFactory() for _ in range(count)]
        for event in events:
            factories.FeedFactory(event=event, scope=scope or self.project)
        # `created` is set on insert, so age the rows afterwards.
        Event.objects.filter(id__in=[event.id for event in events]).update(
            created=timezone.now() - timezone.timedelta(days=age_days)
        )
        return events

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=0)
    def test_zero_keeps_every_event(self):
        self.make_events(3, age_days=3650)

        deleted = tasks.delete_expired_events()

        self.assertEqual(deleted, 0)
        self.assertEqual(Event.objects.count(), 3)
        self.assertEqual(Feed.objects.count(), 3)

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=-5)
    def test_a_negative_period_keeps_every_event(self):
        # A negative period would put the cutoff in the future and empty the
        # table; it is treated as "keep forever" instead.
        self.make_events(2, age_days=0)

        tasks.delete_expired_events()

        self.assertEqual(Event.objects.count(), 2)

    def test_the_default_keeps_events_forever(self):
        self.assertEqual(WaldurCore().EVENT_RETENTION_DAYS, 0)
        self.assertEqual(settings.WALDUR_CORE["EVENT_RETENTION_DAYS"], 0)
        self.make_events(2, age_days=3650)

        tasks.delete_expired_events()

        self.assertEqual(Event.objects.count(), 2)

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=90)
    def test_expired_events_and_their_feeds_are_deleted(self):
        expired = self.make_events(3, age_days=91)
        kept = self.make_events(2, age_days=89)

        deleted = tasks.delete_expired_events()

        self.assertEqual(deleted, 3)
        self.assertFalse(
            Event.objects.filter(id__in=[event.id for event in expired]).exists()
        )
        self.assertEqual(
            set(Event.objects.values_list("id", flat=True)),
            {event.id for event in kept},
        )
        # No dangling feed rows: every remaining feed points at a kept event.
        self.assertEqual(
            set(Feed.objects.values_list("event_id", flat=True)),
            {event.id for event in kept},
        )

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=30)
    def test_deletion_runs_in_batches_until_every_expired_row_is_gone(self):
        self.make_events(7, age_days=31)
        kept = self.make_events(1, age_days=1)

        with CaptureQueriesContext(connection) as queries:
            deleted = tasks.delete_expired_events(batch_size=3)

        self.assertEqual(deleted, 7)
        self.assertEqual(list(Event.objects.values_list("id", flat=True)), [kept[0].id])
        self.assertEqual(Feed.objects.count(), 1)
        event_deletes = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].startswith('DELETE FROM "logging_event"')
        ]
        # 7 expired rows with a batch of 3: batches of 3, 3 and 1.
        self.assertEqual(len(event_deletes), 3)

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=30)
    def test_batches_skip_locked_rows_and_never_read_the_event_payload(self):
        # Overlapping runs (manual + beat) take disjoint batches instead of
        # contending for the same rows, and the cascade loads only the ids.
        self.make_events(2, age_days=31)

        with CaptureQueriesContext(connection) as queries:
            tasks.delete_expired_events()

        sql = [query["sql"] for query in queries.captured_queries]
        self.assertTrue(any("FOR UPDATE SKIP LOCKED" in query for query in sql))
        self.assertFalse(any('"logging_event"."context"' in query for query in sql))
        self.assertFalse(any('"logging_event"."message"' in query for query in sql))

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=30)
    def test_the_count_is_events_deleted_not_rows_deleted(self):
        # .delete() also reports the cascaded feed rows; only events count.
        self.make_events(2, age_days=31)
        factories.FeedFactory(event=Event.objects.first(), scope=self.fixture.customer)

        deleted = tasks.delete_expired_events()

        self.assertEqual(deleted, 2)
        self.assertEqual(Feed.objects.count(), 0)

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=30)
    def test_a_completed_run_does_not_queue_another(self):
        self.make_events(4, age_days=31)

        with mock.patch.object(tasks.delete_expired_events, "delay") as delay:
            deleted = tasks.delete_expired_events(batch_size=3)

        self.assertEqual(deleted, 4)
        delay.assert_not_called()

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=30)
    def test_a_run_out_of_time_queues_the_rest(self):
        self.make_events(7, age_days=31)

        with (
            mock.patch.object(tasks, "EVENT_RETENTION_TIME_BUDGET", 0),
            mock.patch.object(tasks.delete_expired_events, "delay") as delay,
            self.assertLogs(tasks.logger, "INFO") as logs,
        ):
            deleted = tasks.delete_expired_events(batch_size=3)

        # One batch per run once the budget is spent, then a fresh task.
        self.assertEqual(deleted, 3)
        self.assertEqual(Event.objects.count(), 4)
        delay.assert_called_once_with(3)
        self.assertIn("Deleted 3 events", "\n".join(logs.output))

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=30)
    def test_the_soft_time_limit_logs_the_partial_count_and_queues_the_rest(self):
        self.make_events(7, age_days=31)
        real_delete = QuerySet.delete
        calls = []

        def delete_then_time_out(queryset):
            calls.append(queryset)
            if len(calls) > 1:
                raise SoftTimeLimitExceeded()
            return real_delete(queryset)

        with (
            mock.patch.object(
                QuerySet, "delete", autospec=True, side_effect=delete_then_time_out
            ),
            mock.patch.object(tasks.delete_expired_events, "delay") as delay,
            self.assertLogs(tasks.logger, "INFO") as logs,
        ):
            deleted = tasks.delete_expired_events(batch_size=3)

        # The first batch is committed; the interrupted one rolled back.
        self.assertEqual(deleted, 3)
        self.assertEqual(Event.objects.count(), 4)
        self.assertEqual(Feed.objects.count(), 4)
        delay.assert_called_once_with(3)
        output = "\n".join(logs.output)
        self.assertIn("soft time limit after deleting 3 events", output)
        self.assertIn("Deleted 3 events", output)

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=30)
    def test_no_progress_before_the_soft_time_limit_does_not_queue_a_loop(self):
        self.make_events(2, age_days=31)

        with (
            mock.patch.object(
                QuerySet, "delete", autospec=True, side_effect=SoftTimeLimitExceeded
            ),
            mock.patch.object(tasks.delete_expired_events, "delay") as delay,
            self.assertLogs(tasks.logger, "ERROR"),
        ):
            deleted = tasks.delete_expired_events()

        self.assertEqual(deleted, 0)
        self.assertEqual(Event.objects.count(), 2)
        delay.assert_not_called()

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=10**6)
    def test_a_period_past_year_one_deletes_nothing_instead_of_failing(self):
        # timezone.now() - timedelta(days=10**6) overflows datetime.
        self.make_events(2, age_days=3650)

        with self.assertLogs(tasks.logger, "WARNING"):
            deleted = tasks.delete_expired_events()

        self.assertEqual(deleted, 0)
        self.assertEqual(Event.objects.count(), 2)

    @override_waldur_core_settings(EVENT_RETENTION_DAYS=30)
    def test_event_list_and_scoped_feed_keep_working_after_a_run(self):
        self.make_events(2, age_days=31)
        kept = self.make_events(2, age_days=1)

        tasks.delete_expired_events()

        url = factories.EventFactory.get_list_url()
        self.client.force_authenticate(self.staff)
        response = self.client.get(url, {"page_size": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {row["uuid"] for row in response.data},
            {event.uuid.hex for event in kept},
        )

        scope_url = structure_factories.ProjectFactory.get_url(self.project)
        self.client.force_authenticate(self.admin)
        response = self.client.get(url, {"scope": scope_url, "page_size": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {row["uuid"] for row in response.data},
            {event.uuid.hex for event in kept},
        )


class EventRetentionSettingTest(test.APITestCase):
    """The retention period is the deployment's decision, not staff's."""

    def test_the_setting_is_not_editable_at_runtime(self):
        self.assertNotIn("EVENT_RETENTION_DAYS", settings.CONSTANCE_CONFIG)

        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = self.client.get("/api/override-settings/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("EVENT_RETENTION_DAYS", response.data)

    def test_the_setting_is_not_public(self):
        response = self.client.get("/api/configuration/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("EVENT_RETENTION_DAYS", response.data.get("WALDUR_CORE", {}))


@unittest.skipUnless(CONTAINER_SETTINGS.exists(), "container settings not available")
class ContainerEventRetentionSettingTest(SimpleTestCase):
    """The image reads the period from WALDUR_EVENT_RETENTION_DAYS."""

    def compose(self, value):
        environ = {**os.environ, "GLOBAL_SECRET_KEY": "test"}
        environ.pop("WALDUR_EVENT_RETENTION_DAYS", None)
        if value is not None:
            environ["WALDUR_EVENT_RETENTION_DAYS"] = value
        with tempfile.TemporaryDirectory() as conf_dir:
            environ["WALDUR_BASE_CONFIG_DIR"] = conf_dir
            return subprocess.run(
                [sys.executable, "-c", PROBE, str(CONTAINER_SETTINGS)],
                env=environ,
                capture_output=True,
                text=True,
                timeout=300,
            )

    def retention_days(self, value):
        result = self.compose(value)
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(result.stdout.strip().splitlines()[-1])

    def test_unset_keeps_events_forever(self):
        self.assertEqual(self.retention_days(None), 0)
        self.assertEqual(self.retention_days(""), 0)

    def test_the_environment_variable_sets_the_period(self):
        self.assertEqual(self.retention_days("365"), 365)

    def test_a_value_that_is_not_a_number_stops_startup(self):
        result = self.compose("a year")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WALDUR_EVENT_RETENTION_DAYS", result.stderr)

    def test_a_period_longer_than_a_century_stops_startup(self):
        # Far larger values would overflow the cutoff date in the daily task.
        self.assertEqual(self.retention_days("36500"), 36500)
        result = self.compose("36501")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WALDUR_EVENT_RETENTION_DAYS", result.stderr)


class EventRetentionScheduleTest(SimpleTestCase):
    def test_task_is_scheduled_and_listed_by_print_scheduled_jobs(self):
        entry = settings.CELERY_BEAT_SCHEDULE["delete-expired-events"]
        self.assertEqual(entry["task"], "waldur_core.logging.delete_expired_events")

        out = StringIO()
        call_command("print_scheduled_jobs", stdout=out)

        self.assertIn("waldur_core.logging.delete_expired_events", out.getvalue())
