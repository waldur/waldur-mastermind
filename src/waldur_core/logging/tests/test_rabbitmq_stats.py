from unittest.mock import patch

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.logging.exceptions import RabbitMQError
from waldur_core.structure.tests import fixtures as structure_fixtures


@ddt
class RabbitMQStatsGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.url = "/api/rabbitmq-stats/"

    @data("staff", "global_support")
    @patch("waldur_core.logging.views.backend.RabbitMQManagementBackend")
    def test_support_user_can_access_stats(self, user, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.list_all_subscription_queues.return_value = []

        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("vhosts", response.data)
        self.assertIn("total_messages", response.data)
        self.assertIn("total_queues", response.data)

    @data("owner", "user")
    def test_regular_user_cannot_access_stats(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_cannot_access_stats(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("waldur_core.logging.views.backend.RabbitMQManagementBackend")
    def test_stats_returns_enriched_queue_data(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.list_all_subscription_queues.return_value = [
            {
                "vhost": "abc123def456",
                "queues": [
                    {
                        "name": "subscription_aabb1122_offering_ccdd3344_resource",
                        "messages": 100,
                        "messages_ready": 90,
                        "messages_unacknowledged": 10,
                        "consumers": 1,
                        "queue_type": "quorum",
                    }
                ],
                "total_messages": 100,
            }
        ]

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["vhosts"]), 1)
        self.assertEqual(response.data["total_messages"], 100)
        self.assertEqual(response.data["total_queues"], 1)

        queue = response.data["vhosts"][0]["queues"][0]
        self.assertEqual(queue["subscription_uuid"], "aabb1122")
        self.assertEqual(queue["offering_uuid"], "ccdd3344")
        self.assertEqual(queue["object_type"], "resource")
        self.assertEqual(queue["queue_kind"], "legacy")
        self.assertIsNone(queue["consumer_uuid"])
        # The classification lives in queue_kind, so RabbitMQ's own value survives.
        self.assertEqual(queue["queue_type"], "quorum")

    @patch("waldur_core.logging.views.backend.RabbitMQManagementBackend")
    def test_consumer_queue_is_classified_and_parsed(self, mock_backend_class):
        consumer_uuid = "11112222333344445555666677778888"
        mock_backend = mock_backend_class.return_value
        mock_backend.list_all_subscription_queues.return_value = [
            {
                "vhost": "abc123def456",
                "queues": [
                    {
                        "name": f"consumer_{consumer_uuid}",
                        "messages": 5,
                        "messages_ready": 5,
                        "messages_unacknowledged": 0,
                        "consumers": 1,
                        "queue_type": "classic",
                    },
                    # The backend only lists subscription_* and consumer_*
                    # queues, so an unclassifiable name is a malformed one.
                    {
                        "name": "subscription_bad",
                        "messages": 0,
                        "messages_ready": 0,
                        "messages_unacknowledged": 0,
                        "consumers": 0,
                        "queue_type": "classic",
                    },
                ],
                "total_messages": 5,
            }
        ]

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        consumer_queue, other_queue = response.data["vhosts"][0]["queues"]

        self.assertEqual(consumer_queue["queue_kind"], "consumer")
        self.assertEqual(consumer_queue["consumer_uuid"], consumer_uuid)
        self.assertEqual(consumer_queue["queue_type"], "classic")
        self.assertIsNone(consumer_queue["subscription_uuid"])

        self.assertEqual(other_queue["queue_kind"], "unknown")
        self.assertIsNone(other_queue["consumer_uuid"])
        self.assertEqual(other_queue["queue_type"], "classic")

    @patch("waldur_core.logging.views.backend.RabbitMQManagementBackend")
    def test_stats_handles_backend_error(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.list_all_subscription_queues.side_effect = RabbitMQError(
            "Connection failed"
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("error", response.data)


@ddt
class RabbitMQStatsPurgeTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.url = "/api/rabbitmq-stats/"

    @patch("waldur_core.logging.views.backend.RabbitMQManagementBackend")
    def test_staff_can_purge_specific_queue(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.list_queues.return_value = [
            {"name": "test_queue", "messages": 100}
        ]
        mock_backend.purge_queue.return_value = 0

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, data={"vhost": "test_vhost", "queue_name": "test_queue"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["purged_queues"], 1)
        self.assertEqual(response.data["purged_messages"], 100)
        mock_backend.purge_queue.assert_called_once_with("test_vhost", "test_queue")

    @patch("waldur_core.logging.views.backend.RabbitMQManagementBackend")
    def test_staff_can_purge_queues_by_pattern(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.list_queues.return_value = [
            {"name": "subscription_abc_offering_def_resource", "messages": 100},
            {"name": "subscription_xyz_offering_123_resource", "messages": 50},
            {"name": "subscription_abc_offering_def_order", "messages": 25},
        ]
        mock_backend.purge_queue.return_value = 0

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, data={"vhost": "test_vhost", "queue_pattern": "*_resource"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["purged_queues"], 2)
        self.assertEqual(response.data["purged_messages"], 150)

    @patch("waldur_core.logging.views.backend.RabbitMQManagementBackend")
    def test_staff_can_purge_all_subscription_queues(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.list_all_subscription_queues.return_value = [
            {
                "vhost": "vhost1",
                "queues": [
                    {"name": "subscription_abc_offering_def_resource", "messages": 100}
                ],
                "total_messages": 100,
            },
            {
                "vhost": "vhost2",
                "queues": [
                    {"name": "subscription_xyz_offering_123_order", "messages": 50}
                ],
                "total_messages": 50,
            },
        ]
        mock_backend.purge_queue.return_value = 0

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, data={"purge_all_subscription_queues": True}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["purged_queues"], 2)
        self.assertEqual(response.data["purged_messages"], 150)

    def test_support_user_cannot_purge_queues(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.post(
            self.url, data={"vhost": "test_vhost", "queue_name": "test_queue"}
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("owner", "user")
    def test_regular_user_cannot_purge_queues(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.url, data={"vhost": "test_vhost", "queue_name": "test_queue"}
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_cannot_purge_queues(self):
        response = self.client.post(
            self.url, data={"vhost": "test_vhost", "queue_name": "test_queue"}
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_purge_requires_valid_parameters(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, data={})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)

    @patch("waldur_core.logging.views.backend.RabbitMQManagementBackend")
    def test_purge_queue_not_found(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.list_queues.return_value = []

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, data={"vhost": "test_vhost", "queue_name": "nonexistent_queue"}
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("error", response.data)

    @patch("waldur_core.logging.views.backend.RabbitMQManagementBackend")
    def test_purge_handles_backend_error(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.list_queues.side_effect = RabbitMQError("Connection failed")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, data={"vhost": "test_vhost", "queue_name": "test_queue"}
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("error", response.data)
