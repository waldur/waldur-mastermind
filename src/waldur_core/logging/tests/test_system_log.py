import datetime

from constance.test.unittest import override_config
from ddt import data, ddt
from django.utils import timezone
from rest_framework import status, test

from waldur_core.logging.log import DatabaseLogHandler
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
        from waldur_core.logging.models import SystemLog
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
