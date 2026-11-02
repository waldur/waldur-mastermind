import datetime
import logging
import os
import sys
from unittest import mock

from ddt import data, ddt
from django.db import DEFAULT_DB_ALIAS, OperationalError, connections, transaction
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.tests.helpers import dropped_connection, lost_default_connection
from waldur_core.logging.log import DatabaseLogHandler, _close_lost_connection
from waldur_core.logging.models import SystemLog
from waldur_core.logging.tests.factories import SystemLogFactory
from waldur_core.structure.tests import fixtures as structure_fixtures


@ddt
class SystemLogPermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        SystemLogFactory()
        self.url = SystemLogFactory.get_list_url()

    @data("staff", "global_support")
    def test_staff_and_support_can_list(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data))

    @data("owner", "user")
    def test_regular_users_get_forbidden(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_gets_unauthorized(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class SystemLogStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        SystemLogFactory(source="api", instance="pod-1")
        SystemLogFactory(source="api", instance="pod-1")
        SystemLogFactory(source="worker", instance="pod-2")
        self.url = SystemLogFactory.get_stats_url()

    @data("staff", "global_support")
    def test_stats_returns_aggregated_counts(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("instances", response.data)
        self.assertIn("total_size_bytes", response.data)
        self.assertIn("total_size_mb", response.data)
        # Should have 2 groups: api/pod-1 and worker/pod-2
        self.assertEqual(len(response.data["instances"]), 2)

    @data("owner", "user")
    def test_stats_forbidden_for_regular_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class SystemLogInstancesTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        SystemLogFactory(source="api", instance="pod-a")
        SystemLogFactory(source="worker", instance="pod-b")
        self.url = SystemLogFactory.get_instances_url()

    @data("staff", "global_support")
    def test_instances_returns_known_pods(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        instances = {(r["source"], r["instance"]) for r in response.data}
        self.assertIn(("api", "pod-a"), instances)
        self.assertIn(("worker", "pod-b"), instances)

    @data("owner", "user")
    def test_instances_forbidden_for_regular_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SystemLogFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.log1 = SystemLogFactory(
            source="api",
            level="ERROR",
            level_number=40,
            instance="pod-1",
            logger_name="waldur_core.server.views",
        )
        self.log2 = SystemLogFactory(
            source="worker",
            level="INFO",
            level_number=20,
            instance="pod-2",
            logger_name="celery.worker",
        )
        self.url = SystemLogFactory.get_list_url()

    def _get(self, **params):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.get(self.url, params)

    def test_filter_by_source(self):
        response = self._get(source="api")
        ids = {r["id"] for r in response.data}
        self.assertIn(self.log1.pk, ids)
        self.assertNotIn(self.log2.pk, ids)

    def test_filter_by_level_gte(self):
        response = self._get(level_gte=30)
        ids = {r["id"] for r in response.data}
        self.assertIn(self.log1.pk, ids)
        self.assertNotIn(self.log2.pk, ids)

    def test_filter_by_instance(self):
        response = self._get(instance="pod-1")
        ids = {r["id"] for r in response.data}
        self.assertIn(self.log1.pk, ids)
        self.assertNotIn(self.log2.pk, ids)

    def test_filter_by_logger_name(self):
        response = self._get(logger_name="waldur_core")
        ids = {r["id"] for r in response.data}
        self.assertIn(self.log1.pk, ids)
        self.assertNotIn(self.log2.pk, ids)


class DatabaseLogHandlerScrubTest(test.APISimpleTestCase):
    def setUp(self):
        self.handler = DatabaseLogHandler()

    def test_scrub_password_equals(self):
        text = "Connecting with password=s3cret123 to db"
        result = self.handler._scrub(text)
        self.assertNotIn("s3cret123", result)
        self.assertIn("password=***", result)

    def test_scrub_token_colon(self):
        text = "auth_token: abc123xyz"
        result = self.handler._scrub(text)
        self.assertNotIn("abc123xyz", result)
        self.assertIn("auth_token: ***", result)

    def test_scrub_api_key(self):
        text = "API_KEY=mykey123"
        result = self.handler._scrub(text)
        self.assertNotIn("mykey123", result)
        self.assertIn("API_KEY=***", result)

    def test_scrub_normal_text_unchanged(self):
        text = "Processing 42 items in batch"
        result = self.handler._scrub(text)
        self.assertEqual(text, result)

    def test_scrub_none_returns_none(self):
        result = self.handler._scrub(None)
        self.assertIsNone(result)

    def test_scrub_empty_returns_empty(self):
        result = self.handler._scrub("")
        self.assertEqual("", result)


class CleanupSystemLogsTaskTest(test.APITestCase):
    @override_config(SYSTEM_LOG_ENABLED=True, SYSTEM_LOG_MAX_ROWS_PER_SOURCE=5)
    def test_cleanup_deletes_oldest(self):
        from waldur_core.logging.tasks import cleanup_system_logs

        now = timezone.now()
        for i in range(10):
            log = SystemLogFactory(source="api")
            # Ensure distinct timestamps so cutoff query works correctly
            SystemLog.objects.filter(pk=log.pk).update(
                created=now - datetime.timedelta(seconds=10 - i)
            )

        cleanup_system_logs()

        remaining = SystemLog.objects.filter(source="api").count()
        self.assertLessEqual(remaining, 5)


class DatabaseLogHandlerTest(test.APITestCase):
    """Drive the handler itself.

    The rest of this module builds rows with SystemLogFactory, which writes any
    source directly — so the worker and beat paths looked covered while the only
    code that can populate them was never exercised.
    """

    def emit(self, argv, message="pulled tenant", buffer_size=1):
        handler = DatabaseLogHandler(buffer_size=buffer_size)
        record = logging.LogRecord(
            name="waldur_openstack.tasks",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )
        with mock.patch.object(sys, "argv", argv):
            handler.emit(record)
        return handler

    @override_config(SYSTEM_LOG_ENABLED=True)
    def test_records_from_a_worker_are_stored_as_such(self):
        handler = self.emit(["celery", "-A", "waldur_core.server", "worker"])

        log = SystemLog.objects.get()
        self.assertEqual(log.source, "worker")
        self.assertEqual(log.instance, handler.instance)
        self.assertEqual(log.logger_name, "waldur_openstack.tasks")
        self.assertIn("pulled tenant", log.message)

    @override_config(SYSTEM_LOG_ENABLED=True)
    def test_records_from_beat_are_stored_as_such(self):
        self.emit(["celery", "-A", "waldur_core.server", "beat"])

        self.assertEqual(SystemLog.objects.get().source, "beat")

    @override_config(SYSTEM_LOG_ENABLED=False)
    def test_nothing_is_stored_while_the_feature_is_off(self):
        self.emit(["celery", "-A", "waldur_core.server", "worker"])

        self.assertFalse(SystemLog.objects.exists())

    @override_config(SYSTEM_LOG_ENABLED=True)
    def test_a_forked_child_discards_the_inherited_buffer(self):
        """A prefork pool child inherits the parent's buffer; it must not write it.

        Parent and child are separate processes with separate memory, so this
        and the test below each drive their own handler — one instance cannot
        stand in for both.
        """
        # buffer_size well above one record, so it is still buffered at "fork".
        handler = self.emit(["celery", "worker"], buffer_size=100)
        self.assertFalse(SystemLog.objects.exists())

        with mock.patch("os.getpid", return_value=os.getpid() + 1):
            handler.close()

        self.assertFalse(
            SystemLog.objects.exists(), "child rewrote the parent's buffer"
        )

    @override_config(SYSTEM_LOG_ENABLED=True)
    def test_the_parent_still_flushes_its_own_buffer(self):
        handler = self.emit(["celery", "worker"], buffer_size=100)
        self.assertFalse(SystemLog.objects.exists())

        handler.close()

        self.assertEqual(SystemLog.objects.count(), 1)


class DatabaseLogHandlerLostConnectionTest(SimpleTestCase):
    """A process that logs outside requests and tasks must not keep a lost connection.

    Django replaces an unusable connection only when a request or a task starts
    or ends, and the handler swallows its own failures. So a Celery worker's
    main process whose connection the server had dropped lost every later
    record silently, and handed the lost connection to each pool child it forked.

    Not an APITestCase: that runs each test inside an atomic block, where the
    handler leaves the connection to the transaction's owner.
    """

    databases = {DEFAULT_DB_ALIAS}
    logger_name = "waldur_core.logging.tests.lost_connection"

    def setUp(self):
        self.addCleanup(
            lambda: SystemLog.objects.filter(
                logger_name__startswith=self.logger_name
            ).delete()
        )

    def emit(self, handler, message, logger_name=None):
        handler.emit(
            logging.LogRecord(
                name=logger_name or self.logger_name,
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg=message,
                args=(),
                exc_info=None,
            )
        )

    def stored_messages(self):
        return list(
            SystemLog.objects.filter(logger_name=self.logger_name).values_list(
                "message", flat=True
            )
        )

    @override_config(SYSTEM_LOG_ENABLED=True)
    def test_a_batch_that_hit_a_lost_connection_is_written_on_a_new_one(self):
        handler = DatabaseLogHandler(buffer_size=1)
        wrapper = connections[DEFAULT_DB_ALIAS]

        with lost_default_connection() as lost:
            self.emit(handler, "through the lost connection")

            self.assertIsNot(wrapper.connection, lost)
            self.assertFalse(wrapper.connection.closed)

        messages = self.stored_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("through the lost connection", messages[0])

    @override_config(SYSTEM_LOG_ENABLED=True)
    def test_a_batch_that_finds_the_connection_dropped_is_written_on_a_new_one(self):
        """The usual case: the write itself is the first to meet the dropped
        connection, which psycopg still reported open."""
        handler = DatabaseLogHandler(buffer_size=1)
        wrapper = connections[DEFAULT_DB_ALIAS]

        with lost_default_connection(noticed=False) as lost:
            self.emit(handler, "first through the dropped connection")

            self.assertTrue(lost.closed)
            self.assertIsNot(wrapper.connection, lost)

        messages = self.stored_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("first through the dropped connection", messages[0])

    @override_config(SYSTEM_LOG_ENABLED=True)
    def test_a_batch_is_written_after_django_replaced_the_lost_connection(self):
        """bulk_create makes a batch it splits into several inserts one
        transaction. When that fails on a lost connection, Django closes it and
        opens a new one on its way out, so the handler finds a live connection."""
        handler = DatabaseLogHandler(buffer_size=1)
        wrapper = connections[DEFAULT_DB_ALIAS]
        write = handler._bulk_create

        def in_its_own_transaction(records):
            with transaction.atomic(savepoint=False):
                write(records)

        with lost_default_connection(noticed=False) as lost:
            with mock.patch.object(
                handler, "_bulk_create", side_effect=in_its_own_transaction
            ) as bulk_create:
                self.emit(handler, "through the replaced connection")

            self.assertIsNot(wrapper.connection, lost)

        self.assertEqual(bulk_create.call_count, 2)
        self.assertEqual(len(self.stored_messages()), 1)

    @override_config(SYSTEM_LOG_ENABLED=True)
    def test_a_retry_that_loses_its_connection_too_leaves_none_behind(self):
        handler = DatabaseLogHandler(buffer_size=1)
        wrapper = connections[DEFAULT_DB_ALIAS]
        # The server drops the new connection as well, as while it restarts.
        second = dropped_connection(noticed=False)
        self.addCleanup(second.close)

        with lost_default_connection():
            with mock.patch.object(wrapper, "get_new_connection", return_value=second):
                self.emit(handler, "while the database restarts")

            self.assertTrue(second.closed)
            self.assertIsNone(wrapper.connection)

        self.assertEqual(self.stored_messages(), [])

    @override_config(SYSTEM_LOG_ENABLED=True)
    def test_a_batch_is_dropped_while_the_database_is_still_down(self):
        handler = DatabaseLogHandler(buffer_size=1)
        wrapper = connections[DEFAULT_DB_ALIAS]

        with lost_default_connection():
            with mock.patch.object(
                wrapper,
                "get_new_connection",
                side_effect=OperationalError("the server is still down"),
            ) as connect:
                self.emit(handler, "during the outage")

            self.assertEqual(connect.call_count, 1)
            self.assertIsNone(wrapper.connection)

        self.assertEqual(self.stored_messages(), [])

    def test_a_lost_connection_in_a_transaction_is_left_to_its_owner(self):
        wrapper = connections[DEFAULT_DB_ALIAS]
        with lost_default_connection() as lost:
            with mock.patch.object(wrapper, "in_atomic_block", True):
                self.assertFalse(_close_lost_connection())

            self.assertIs(wrapper.connection, lost)

    @override_config(SYSTEM_LOG_ENABLED=True)
    def test_a_live_connection_survives_a_failed_write(self):
        handler = DatabaseLogHandler(buffer_size=1)
        wrapper = connections[DEFAULT_DB_ALIAS]
        wrapper.ensure_connection()
        live = wrapper.connection

        with mock.patch.object(
            handler, "_bulk_create", wraps=handler._bulk_create
        ) as bulk_create:
            # Longer than the column, so the server rejects the insert.
            self.emit(
                handler, "rejected", logger_name=f"{self.logger_name}.{'x' * 255}"
            )

        self.assertEqual(bulk_create.call_count, 1, "a failed write was retried")
        self.assertIs(wrapper.connection, live)
        self.assertFalse(
            SystemLog.objects.filter(logger_name__startswith=self.logger_name).exists()
        )
