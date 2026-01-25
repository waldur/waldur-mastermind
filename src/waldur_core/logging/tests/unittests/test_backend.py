import unittest
from unittest.mock import MagicMock, patch

from waldur_core.logging.backend import RabbitMQManagementBackend
from waldur_core.logging.exceptions import RabbitMQError


class ListQueuesTest(unittest.TestCase):
    @patch("waldur_core.logging.backend.settings")
    @patch("waldur_core.logging.backend.requests.get")
    def test_list_queues_success(self, mock_get, mock_settings):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "name": "subscription_abc_offering_def_resource",
                "messages": 100,
                "messages_ready": 90,
                "messages_unacknowledged": 10,
                "consumers": 1,
            },
            {
                "name": "subscription_xyz_offering_123_order",
                "messages": 50,
                "messages_ready": 50,
                "messages_unacknowledged": 0,
                "consumers": 0,
            },
        ]
        mock_get.return_value = mock_response

        backend = RabbitMQManagementBackend()
        result = backend.list_queues("test_vhost")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "subscription_abc_offering_def_resource")
        self.assertEqual(result[0]["messages"], 100)
        self.assertEqual(result[0]["messages_ready"], 90)
        self.assertEqual(result[0]["messages_unacknowledged"], 10)
        self.assertEqual(result[0]["consumers"], 1)

    @patch("waldur_core.logging.backend.settings")
    @patch("waldur_core.logging.backend.requests.get")
    def test_list_queues_vhost_not_found(self, mock_get, mock_settings):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        backend = RabbitMQManagementBackend()
        result = backend.list_queues("nonexistent_vhost")

        self.assertEqual(result, [])

    @patch("waldur_core.logging.backend.settings")
    @patch("waldur_core.logging.backend.requests.get")
    def test_list_queues_error(self, mock_get, mock_settings):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_get.return_value = mock_response

        backend = RabbitMQManagementBackend()

        with self.assertRaises(RabbitMQError):
            backend.list_queues("test_vhost")


class PurgeQueueTest(unittest.TestCase):
    @patch("waldur_core.logging.backend.settings")
    @patch("waldur_core.logging.backend.requests.delete")
    def test_purge_queue_success(self, mock_delete, mock_settings):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_delete.return_value = mock_response

        backend = RabbitMQManagementBackend()
        result = backend.purge_queue("test_vhost", "test_queue")

        self.assertEqual(result, 0)
        mock_delete.assert_called_once()

    @patch("waldur_core.logging.backend.settings")
    @patch("waldur_core.logging.backend.requests.delete")
    def test_purge_queue_not_found(self, mock_delete, mock_settings):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_delete.return_value = mock_response

        backend = RabbitMQManagementBackend()
        result = backend.purge_queue("test_vhost", "nonexistent_queue")

        self.assertEqual(result, 0)

    @patch("waldur_core.logging.backend.settings")
    @patch("waldur_core.logging.backend.requests.delete")
    def test_purge_queue_error(self, mock_delete, mock_settings):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_delete.return_value = mock_response

        backend = RabbitMQManagementBackend()

        with self.assertRaises(RabbitMQError):
            backend.purge_queue("test_vhost", "test_queue")


class CreateQueueTest(unittest.TestCase):
    @patch("waldur_core.logging.backend.settings")
    @patch("waldur_core.logging.backend.requests.put")
    def test_create_queue_success(self, mock_put, mock_settings):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_put.return_value = mock_response

        backend = RabbitMQManagementBackend()
        result = backend.create_queue(
            "test_vhost",
            "test_queue",
            durable=True,
            auto_delete=False,
            arguments={"x-max-length": 10000},
        )

        self.assertTrue(result)
        mock_put.assert_called_once()
        call_args = mock_put.call_args
        self.assertIn("test_vhost", call_args[0][0])
        self.assertIn("test_queue", call_args[0][0])
        self.assertEqual(call_args[1]["json"]["durable"], True)
        self.assertEqual(call_args[1]["json"]["auto_delete"], False)
        self.assertEqual(call_args[1]["json"]["arguments"]["x-max-length"], 10000)

    @patch("waldur_core.logging.backend.settings")
    @patch("waldur_core.logging.backend.requests.put")
    def test_create_queue_already_exists(self, mock_put, mock_settings):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_response = MagicMock()
        mock_response.status_code = 204  # Already exists
        mock_put.return_value = mock_response

        backend = RabbitMQManagementBackend()
        result = backend.create_queue("test_vhost", "test_queue")

        self.assertTrue(result)

    @patch("waldur_core.logging.backend.settings")
    @patch("waldur_core.logging.backend.requests.put")
    def test_create_queue_precondition_failed(self, mock_put, mock_settings):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_response = MagicMock()
        mock_response.status_code = 409  # Precondition failed (args mismatch)
        mock_response.text = "PRECONDITIONS_FAILED - inequivalent arg"
        mock_put.return_value = mock_response

        backend = RabbitMQManagementBackend()
        result = backend.create_queue("test_vhost", "test_queue")

        self.assertFalse(result)

    @patch("waldur_core.logging.backend.settings")
    @patch("waldur_core.logging.backend.requests.put")
    def test_create_queue_connection_error(self, mock_put, mock_settings):
        import requests

        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_put.side_effect = requests.RequestException("Connection failed")

        backend = RabbitMQManagementBackend()
        result = backend.create_queue("test_vhost", "test_queue")

        self.assertFalse(result)


class ListAllSubscriptionQueuesTest(unittest.TestCase):
    @patch("waldur_core.logging.backend.settings")
    @patch.object(RabbitMQManagementBackend, "list_rabbitmq_virtual_hosts")
    @patch.object(RabbitMQManagementBackend, "list_queues")
    def test_list_all_subscription_queues_success(
        self, mock_list_queues, mock_list_vhosts, mock_settings
    ):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_list_vhosts.return_value = ["/", "vhost1", "vhost2"]
        mock_list_queues.side_effect = [
            # vhost1 queues
            [
                {"name": "subscription_abc_offering_def_resource", "messages": 100},
                {"name": "celery", "messages": 5},  # Should be filtered out
            ],
            # vhost2 queues
            [
                {"name": "subscription_xyz_offering_123_order", "messages": 50},
            ],
        ]

        backend = RabbitMQManagementBackend()
        result = backend.list_all_subscription_queues()

        self.assertEqual(len(result), 2)
        # Check first vhost
        self.assertEqual(result[0]["vhost"], "vhost1")
        self.assertEqual(len(result[0]["queues"]), 1)
        self.assertEqual(result[0]["total_messages"], 100)
        # Check second vhost
        self.assertEqual(result[1]["vhost"], "vhost2")
        self.assertEqual(len(result[1]["queues"]), 1)
        self.assertEqual(result[1]["total_messages"], 50)

    @patch("waldur_core.logging.backend.settings")
    @patch.object(RabbitMQManagementBackend, "list_rabbitmq_virtual_hosts")
    @patch.object(RabbitMQManagementBackend, "list_queues")
    def test_list_all_subscription_queues_skips_default_vhost(
        self, mock_list_queues, mock_list_vhosts, mock_settings
    ):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_list_vhosts.return_value = ["/"]
        # Should not be called since "/" is skipped
        mock_list_queues.return_value = []

        backend = RabbitMQManagementBackend()
        result = backend.list_all_subscription_queues()

        self.assertEqual(len(result), 0)
        mock_list_queues.assert_not_called()

    @patch("waldur_core.logging.backend.settings")
    @patch.object(RabbitMQManagementBackend, "list_rabbitmq_virtual_hosts")
    @patch.object(RabbitMQManagementBackend, "list_queues")
    def test_list_all_subscription_queues_handles_errors(
        self, mock_list_queues, mock_list_vhosts, mock_settings
    ):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_list_vhosts.return_value = ["vhost1", "vhost2"]
        mock_list_queues.side_effect = [
            RabbitMQError("Connection failed"),  # First vhost fails
            [{"name": "subscription_xyz_offering_123_order", "messages": 50}],
        ]

        backend = RabbitMQManagementBackend()
        result = backend.list_all_subscription_queues()

        # Should only return data for vhost2 since vhost1 failed
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["vhost"], "vhost2")

    @patch("waldur_core.logging.backend.settings")
    @patch.object(RabbitMQManagementBackend, "list_rabbitmq_virtual_hosts")
    @patch.object(RabbitMQManagementBackend, "list_queues")
    def test_list_all_subscription_queues_empty_vhosts(
        self, mock_list_queues, mock_list_vhosts, mock_settings
    ):
        mock_settings.RABBITMQ = {
            "HOST": "localhost",
            "MANAGEMENT_PORT": 15672,
            "USER": "guest",
            "PASSWORD": "guest",
        }
        mock_list_vhosts.return_value = ["vhost1"]
        mock_list_queues.return_value = [
            {"name": "celery", "messages": 5}  # No subscription queues
        ]

        backend = RabbitMQManagementBackend()
        result = backend.list_all_subscription_queues()

        # Should return empty since no subscription queues found
        self.assertEqual(len(result), 0)
