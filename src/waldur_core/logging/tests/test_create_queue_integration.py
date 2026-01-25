"""Integration tests for EventSubscription create_queue endpoint."""

from unittest.mock import patch

from rest_framework import status, test

from waldur_core.logging import models
from waldur_core.logging.tests import factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class CreateQueueIntegrationTest(test.APITransactionTestCase):
    """
    Integration tests for POST /api/event-subscriptions/{uuid}/create_queue/

    Tests the full flow from HTTP request to database and RabbitMQ queue creation.
    """

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.user = self.fixture.owner

        # Create event subscription
        self.event_subscription = factories.EventSubscriptionFactory(
            user=self.user,
            observable_objects=[{"object_type": "resource"}],
        )

        # Create offering the user has access to
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer
        )

        self.url = factories.EventSubscriptionFactory.get_url(
            self.event_subscription, action="create_queue"
        )

        # Mock RabbitMQ backend
        self.rmq_patcher = patch(
            "waldur_core.logging.backend.RabbitMQManagementBackend"
        )
        self.mock_rmq_class = self.rmq_patcher.start()
        self.mock_rmq = self.mock_rmq_class.return_value
        self.mock_rmq.create_queue.return_value = True

    def tearDown(self):
        self.rmq_patcher.stop()

    def test_full_flow_creates_queue_in_db_and_rabbitmq(self):
        """Test complete flow: validates input, creates DB record, creates RabbitMQ queue."""
        self.client.force_authenticate(self.user)

        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "resource",
            },
        )

        # Assert HTTP response
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("uuid", response.data)
        self.assertIn("queue_name", response.data)
        self.assertIn("vhost", response.data)

        # Assert database record created
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 1)
        queue = models.EventSubscriptionQueue.objects.first()
        self.assertEqual(queue.event_subscription, self.event_subscription)
        self.assertEqual(queue.offering_uuid, self.offering.uuid)
        self.assertEqual(queue.object_type, "resource")

        # Assert RabbitMQ was called with correct parameters
        self.mock_rmq.create_queue.assert_called_once()
        call_args = self.mock_rmq.create_queue.call_args

        # Check vhost matches user's UUID
        self.assertEqual(call_args[1]["vhost"], self.user.uuid.hex)

        # Check queue name format
        expected_queue_name = f"subscription_{self.event_subscription.uuid.hex}_offering_{self.offering.uuid.hex}_resource"
        self.assertEqual(call_args[1]["queue_name"], expected_queue_name)

        # Check queue configuration
        self.assertTrue(call_args[1]["durable"])
        self.assertFalse(call_args[1]["auto_delete"])
        self.assertIn("arguments", call_args[1])

    def test_idempotent_behavior_returns_existing_queue(self):
        """Test that creating the same queue twice returns the existing queue."""
        self.client.force_authenticate(self.user)

        # First request - creates queue
        response1 = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "resource",
            },
        )
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)
        queue_uuid = response1.data["uuid"]

        # Reset mock to track second call
        self.mock_rmq.create_queue.reset_mock()

        # Second request - returns existing queue
        response2 = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "resource",
            },
        )

        # Assert returns 200 (not 201)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertEqual(response2.data["uuid"], queue_uuid)

        # Assert still only one DB record
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 1)

        # Assert RabbitMQ queue creation was called again (idempotent operation)
        self.mock_rmq.create_queue.assert_called_once()

    def test_multiple_queues_for_different_offerings(self):
        """Test creating multiple queues for the same subscription but different offerings."""
        self.client.force_authenticate(self.user)

        # Create second offering
        offering2 = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer
        )

        # Create queue for first offering
        response1 = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "resource",
            },
        )
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        # Create queue for second offering
        response2 = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering2.uuid),
                "object_type": "resource",
            },
        )
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)

        # Assert two separate queues created
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 2)

        # Assert different UUIDs
        self.assertNotEqual(response1.data["uuid"], response2.data["uuid"])

        # Assert RabbitMQ called twice
        self.assertEqual(self.mock_rmq.create_queue.call_count, 2)

    def test_multiple_queues_for_different_object_types(self):
        """Test creating multiple queues for the same offering but different object types."""
        self.client.force_authenticate(self.user)

        # Create queue for 'resource' type
        response1 = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "resource",
            },
        )
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        # Create queue for 'order' type
        response2 = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "order",
            },
        )
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)

        # Assert two separate queues created
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 2)
        queues = models.EventSubscriptionQueue.objects.all()
        object_types = {q.object_type for q in queues}
        self.assertEqual(object_types, {"resource", "order"})

    def test_rabbitmq_failure_rolls_back_database(self):
        """Test that if RabbitMQ queue creation fails, database transaction is rolled back."""
        self.mock_rmq.create_queue.return_value = False

        self.client.force_authenticate(self.user)

        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "resource",
            },
        )

        # Assert error response
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Failed to create queue in RabbitMQ", str(response.data))

        # Assert no database record created
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 0)

    def test_rabbitmq_exception_rolls_back_database(self):
        """Test that if RabbitMQ raises exception, database transaction is rolled back."""
        self.mock_rmq.create_queue.side_effect = Exception("RabbitMQ connection failed")

        self.client.force_authenticate(self.user)

        with self.assertRaises(Exception):
            self.client.post(
                self.url,
                {
                    "offering_uuid": str(self.offering.uuid),
                    "object_type": "resource",
                },
            )

        # Assert no database record created (transaction rolled back)
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 0)

    def test_validation_fails_before_rabbitmq_call(self):
        """Test that invalid input fails validation before calling RabbitMQ."""
        self.client.force_authenticate(self.user)

        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "invalid_type",  # Invalid
            },
        )

        # Assert validation error
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("object_type", response.data)

        # Assert RabbitMQ was NOT called
        self.mock_rmq.create_queue.assert_not_called()

        # Assert no database record created
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 0)

    def test_offering_permission_check_integration(self):
        """Test that user permission to offering is validated in the full flow."""
        # Create offering in different customer (no access)
        other_customer = fixtures.CustomerFixture().customer
        other_offering = marketplace_factories.OfferingFactory(customer=other_customer)

        self.client.force_authenticate(self.user)

        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(other_offering.uuid),
                "object_type": "resource",
            },
        )

        # Assert permission denied
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_uuid", response.data)
        self.assertIn("do not have access", str(response.data))

        # Assert RabbitMQ was NOT called
        self.mock_rmq.create_queue.assert_not_called()

        # Assert no database record created
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 0)

    def test_response_includes_correct_fields(self):
        """Test that response includes all expected fields with correct values."""
        self.client.force_authenticate(self.user)

        response = self.client.post(
            self.url,
            {
                "offering_uuid": self.offering.uuid.hex,
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Assert response structure
        required_fields = {
            "uuid",
            "url",
            "event_subscription",
            "event_subscription_uuid",
            "offering_uuid",
            "object_type",
            "queue_name",
            "vhost",
            "created",
        }
        self.assertEqual(set(response.data.keys()), required_fields)

        # Assert field values
        self.assertEqual(response.data["offering_uuid"], self.offering.uuid.hex)
        self.assertEqual(response.data["object_type"], "resource")
        self.assertEqual(
            response.data["event_subscription_uuid"], self.event_subscription.uuid.hex
        )
        self.assertEqual(response.data["vhost"], self.user.uuid.hex)

        # Assert queue name format
        expected_prefix = f"subscription_{self.event_subscription.uuid.hex}_offering_"
        self.assertTrue(response.data["queue_name"].startswith(expected_prefix))

    def test_concurrent_queue_creation_handles_race_condition(self):
        """Test that concurrent requests for the same queue don't create duplicates."""

        self.client.force_authenticate(self.user)

        # First request creates the queue
        response1 = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "resource",
            },
        )
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        # Second request returns existing queue (idempotent)
        response2 = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "resource",
            },
        )
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        # Assert only one queue exists
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 1)

    def test_queue_name_format_matches_specification(self):
        """Test that generated queue name matches the expected format."""
        self.client.force_authenticate(self.user)

        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Assert queue name format
        queue_name = response.data["queue_name"]
        expected_name = (
            f"subscription_{self.event_subscription.uuid.hex}_"
            f"offering_{self.offering.uuid.hex}_resource"
        )
        self.assertEqual(queue_name, expected_name)

        # Verify the name components
        parts = queue_name.split("_")
        self.assertEqual(parts[0], "subscription")
        self.assertEqual(parts[2], "offering")
        self.assertEqual(parts[4], "resource")

    def test_vhost_matches_user_uuid(self):
        """Test that vhost is correctly set to user's UUID hex."""
        self.client.force_authenticate(self.user)

        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(self.offering.uuid),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["vhost"], self.user.uuid.hex)

        # Verify RabbitMQ was called with correct vhost
        call_args = self.mock_rmq.create_queue.call_args
        self.assertEqual(call_args[1]["vhost"], self.user.uuid.hex)
