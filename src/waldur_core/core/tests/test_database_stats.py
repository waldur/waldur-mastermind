from rest_framework import status
from rest_framework.test import APITransactionTestCase

from waldur_core.structure.tests import fixtures


class DatabaseStatsTest(APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.url = "/api/database-stats/"

    def test_regular_user_cannot_access_database_stats(self):
        # Arrange
        self.client.force_authenticate(user=self.fixture.user)

        # Act
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_cannot_access_database_stats(self):
        # Act
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_staff_user_can_access_database_stats(self):
        # Arrange
        self.client.force_authenticate(user=self.fixture.staff)

        # Act
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self._assert_response_structure(response.data)

    def test_support_user_can_access_database_stats(self):
        # Arrange
        support_user = self.fixture.user
        support_user.is_support = True
        support_user.save()
        self.client.force_authenticate(user=support_user)

        # Act
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self._assert_response_structure(response.data)

    def _assert_response_structure(self, data):
        """Verify the response contains all expected sections."""
        # Check top-level keys
        expected_keys = {
            "table_stats",
            "connections",
            "database_size",
            "cache_performance",
            "transactions",
            "locks",
            "maintenance",
            "active_queries",
            "query_performance",
            "replication",
        }
        self.assertEqual(set(data.keys()), expected_keys)

        # Check connections structure
        connections = data["connections"]
        self.assertIn("active", connections)
        self.assertIn("idle", connections)
        self.assertIn("idle_in_transaction", connections)
        self.assertIn("waiting", connections)
        self.assertIn("max_connections", connections)
        self.assertIn("utilization_percent", connections)

        # Check database_size structure
        db_size = data["database_size"]
        self.assertIn("database_name", db_size)
        self.assertIn("total_size_bytes", db_size)
        self.assertIn("data_size_bytes", db_size)
        self.assertIn("index_size_bytes", db_size)

        # Check cache_performance structure
        cache = data["cache_performance"]
        self.assertIn("buffer_cache_hit_ratio", cache)
        self.assertIn("shared_buffers", cache)
        self.assertIn("effective_cache_size", cache)
        self.assertIn("index_hit_ratio", cache)

        # Check transactions structure
        transactions = data["transactions"]
        self.assertIn("committed", transactions)
        self.assertIn("rolled_back", transactions)
        self.assertIn("rollback_ratio_percent", transactions)
        self.assertIn("deadlocks", transactions)

        # Check locks structure
        locks = data["locks"]
        self.assertIn("total_locks", locks)
        self.assertIn("waiting_locks", locks)
        self.assertIn("access_exclusive_locks", locks)

        # Check maintenance structure
        maintenance = data["maintenance"]
        self.assertIn("oldest_transaction_age", maintenance)
        self.assertIn("tables_needing_vacuum", maintenance)
        self.assertIn("total_dead_tuples", maintenance)
        self.assertIn("total_live_tuples", maintenance)
        self.assertIn("dead_tuple_ratio_percent", maintenance)

        # Check active_queries structure
        active_queries = data["active_queries"]
        self.assertIn("count", active_queries)
        self.assertIn("longest_duration_seconds", active_queries)
        self.assertIn("waiting_on_locks", active_queries)
        self.assertIn("queries", active_queries)
        self.assertIsInstance(active_queries["queries"], list)

        # Check query_performance structure
        query_perf = data["query_performance"]
        self.assertIn("seq_scan_count", query_perf)
        self.assertIn("seq_scan_rows", query_perf)
        self.assertIn("index_scan_count", query_perf)
        self.assertIn("index_scan_rows", query_perf)
        self.assertIn("temp_files_count", query_perf)
        self.assertIn("temp_files_bytes", query_perf)

        # Check replication structure
        replication = data["replication"]
        self.assertIn("is_replica", replication)
        self.assertIn("wal_bytes", replication)
        self.assertIn("replication_lag_bytes", replication)

    def test_table_stats_returns_list(self):
        # Arrange
        self.client.force_authenticate(user=self.fixture.staff)

        # Act
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data["table_stats"], list)

        # Check table_stats item structure if not empty
        if response.data["table_stats"]:
            table = response.data["table_stats"][0]
            self.assertIn("table_name", table)
            self.assertIn("total_size", table)
            self.assertIn("data_size", table)
            self.assertIn("external_size", table)

    def test_connection_utilization_is_percentage(self):
        # Arrange
        self.client.force_authenticate(user=self.fixture.staff)

        # Act
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        utilization = response.data["connections"]["utilization_percent"]
        self.assertGreaterEqual(utilization, 0)
        self.assertLessEqual(utilization, 100)

    def test_cache_hit_ratio_is_valid(self):
        # Arrange
        self.client.force_authenticate(user=self.fixture.staff)

        # Act
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        cache = response.data["cache_performance"]
        # buffer_cache_hit_ratio can be None if no reads have occurred
        if cache["buffer_cache_hit_ratio"] is not None:
            self.assertGreaterEqual(cache["buffer_cache_hit_ratio"], 0)
            self.assertLessEqual(cache["buffer_cache_hit_ratio"], 100)
