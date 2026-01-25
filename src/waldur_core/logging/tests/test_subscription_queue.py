import uuid
from unittest.mock import patch

from rest_framework import test

from waldur_core.logging import models, tasks
from waldur_core.logging.tests import factories
from waldur_core.structure.tests import fixtures


class EventSubscriptionQueueModelTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.event_subscription = factories.EventSubscriptionFactory(
            user=self.fixture.owner,
            observable_objects=[{"object_type": "resource"}],
        )

    def test_queue_name_property(self):
        offering_uuid = uuid.uuid4()
        queue = models.EventSubscriptionQueue(
            event_subscription=self.event_subscription,
            offering_uuid=offering_uuid,
            object_type="resource",
        )

        expected_name = f"subscription_{self.event_subscription.uuid.hex}_offering_{offering_uuid.hex}_resource"
        self.assertEqual(queue.queue_name, expected_name)

    def test_vhost_property(self):
        queue = models.EventSubscriptionQueue(
            event_subscription=self.event_subscription,
            offering_uuid=uuid.uuid4(),
            object_type="order",
        )

        expected_vhost = self.event_subscription.user.uuid.hex
        self.assertEqual(queue.vhost, expected_vhost)

    def test_unique_together_constraint(self):
        from django.db import IntegrityError, transaction

        offering_uuid = uuid.uuid4()

        # Create first queue
        models.EventSubscriptionQueue.objects.create(
            event_subscription=self.event_subscription,
            offering_uuid=offering_uuid,
            object_type="resource",
        )

        # Attempt to create duplicate
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                models.EventSubscriptionQueue.objects.create(
                    event_subscription=self.event_subscription,
                    offering_uuid=offering_uuid,
                    object_type="resource",
                )

    def test_different_object_types_allowed(self):
        offering_uuid = uuid.uuid4()

        # Create queues with different object types
        queue1 = models.EventSubscriptionQueue.objects.create(
            event_subscription=self.event_subscription,
            offering_uuid=offering_uuid,
            object_type="resource",
        )
        queue2 = models.EventSubscriptionQueue.objects.create(
            event_subscription=self.event_subscription,
            offering_uuid=offering_uuid,
            object_type="order",
        )

        self.assertNotEqual(queue1.pk, queue2.pk)
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 2)


class EventSubscriptionQueueSerializerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.event_subscription = factories.EventSubscriptionFactory(
            user=self.fixture.owner,
            observable_objects=[{"object_type": "resource"}],
        )

    @patch("waldur_core.logging.serializers.backend.RabbitMQManagementBackend")
    def test_create_queue_success(self, mock_backend_class):
        from waldur_core.logging.serializers import (
            EventSubscriptionQueueCreateSerializer,
        )
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        mock_backend = mock_backend_class.return_value
        mock_backend.create_queue.return_value = True

        # Create an offering the user has access to
        offering = marketplace_factories.OfferingFactory(customer=self.fixture.customer)

        class MockRequest:
            user = self.fixture.owner

        serializer = EventSubscriptionQueueCreateSerializer(
            data={
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
            context={
                "request": MockRequest(),
                "event_subscription": self.event_subscription,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        queue = serializer.save()

        self.assertEqual(queue.event_subscription, self.event_subscription)
        self.assertEqual(queue.offering_uuid, offering.uuid)
        self.assertEqual(queue.object_type, "resource")

        # Verify RabbitMQ backend was called
        mock_backend.create_queue.assert_called_once()

    def test_invalid_object_type_rejected(self):
        from waldur_core.logging.serializers import (
            EventSubscriptionQueueCreateSerializer,
        )

        class MockRequest:
            user = self.fixture.owner

        serializer = EventSubscriptionQueueCreateSerializer(
            data={
                "offering_uuid": str(uuid.uuid4()),
                "object_type": "invalid_type",
            },
            context={
                "request": MockRequest(),
                "event_subscription": self.event_subscription,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("object_type", serializer.errors)

    def test_nonexistent_offering_rejected(self):
        from waldur_core.logging.serializers import (
            EventSubscriptionQueueCreateSerializer,
        )

        class MockRequest:
            user = self.fixture.owner

        serializer = EventSubscriptionQueueCreateSerializer(
            data={
                "offering_uuid": str(uuid.uuid4()),
                "object_type": "resource",
            },
            context={
                "request": MockRequest(),
                "event_subscription": self.event_subscription,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("offering_uuid", serializer.errors)


class EventSubscriptionQueueDeleteSignalTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.event_subscription = factories.EventSubscriptionFactory(
            user=self.fixture.owner,
            observable_objects=[{"object_type": "resource"}],
        )

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_deleting_queue_record_calls_rabbitmq_delete(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.delete_queue.return_value = True

        offering_uuid = uuid.uuid4()
        queue = models.EventSubscriptionQueue.objects.create(
            event_subscription=self.event_subscription,
            offering_uuid=offering_uuid,
            object_type="resource",
        )

        expected_vhost = self.event_subscription.user.uuid.hex
        expected_queue_name = queue.queue_name

        queue.delete()

        mock_backend.delete_queue.assert_called_once_with(
            expected_vhost, expected_queue_name
        )

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_cascade_delete_cleans_up_rabbitmq_queues(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.delete_queue.return_value = True

        offering_uuid = uuid.uuid4()
        models.EventSubscriptionQueue.objects.create(
            event_subscription=self.event_subscription,
            offering_uuid=offering_uuid,
            object_type="resource",
        )
        models.EventSubscriptionQueue.objects.create(
            event_subscription=self.event_subscription,
            offering_uuid=offering_uuid,
            object_type="order",
        )

        self.event_subscription.delete()

        self.assertEqual(mock_backend.delete_queue.call_count, 2)

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_rabbitmq_failure_does_not_block_deletion(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.delete_queue.side_effect = Exception("RabbitMQ unavailable")

        queue = models.EventSubscriptionQueue.objects.create(
            event_subscription=self.event_subscription,
            offering_uuid=uuid.uuid4(),
            object_type="resource",
        )

        # Should not raise even though RabbitMQ call fails
        queue.delete()

        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 0)


class CleanupOrphanSubscriptionQueuesTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.event_subscription = factories.EventSubscriptionFactory(
            user=self.fixture.owner,
            observable_objects=[{"object_type": "resource"}],
        )

    @patch("waldur_core.logging.tasks.backend.RabbitMQManagementBackend")
    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_orphan_queues_are_deleted(
        self, mock_handler_backend_class, mock_task_backend_class
    ):
        mock_task_backend = mock_task_backend_class.return_value
        vhost = self.event_subscription.user.uuid.hex

        # RabbitMQ reports a queue that has no matching DB record
        mock_task_backend.list_all_subscription_queues.return_value = [
            {
                "vhost": vhost,
                "queues": [
                    {"name": "subscription_deadbeef_offering_cafebabe_resource"},
                ],
                "total_messages": 0,
            }
        ]
        mock_task_backend.delete_queue.return_value = True

        tasks.cleanup_orphan_subscription_queues()

        mock_task_backend.delete_queue.assert_called_once_with(
            vhost, "subscription_deadbeef_offering_cafebabe_resource"
        )

    @patch("waldur_core.logging.tasks.backend.RabbitMQManagementBackend")
    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_known_queues_are_not_deleted(
        self, mock_handler_backend_class, mock_task_backend_class
    ):
        mock_task_backend = mock_task_backend_class.return_value

        offering_uuid = uuid.uuid4()
        queue = models.EventSubscriptionQueue.objects.create(
            event_subscription=self.event_subscription,
            offering_uuid=offering_uuid,
            object_type="resource",
        )
        vhost = self.event_subscription.user.uuid.hex

        # RabbitMQ reports the queue that matches a DB record
        mock_task_backend.list_all_subscription_queues.return_value = [
            {
                "vhost": vhost,
                "queues": [
                    {"name": queue.queue_name},
                ],
                "total_messages": 5,
            }
        ]

        tasks.cleanup_orphan_subscription_queues()

        mock_task_backend.delete_queue.assert_not_called()

    @patch("waldur_core.logging.tasks.backend.RabbitMQManagementBackend")
    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_mixed_queues_only_orphans_deleted(
        self, mock_handler_backend_class, mock_task_backend_class
    ):
        mock_task_backend = mock_task_backend_class.return_value

        offering_uuid = uuid.uuid4()
        queue = models.EventSubscriptionQueue.objects.create(
            event_subscription=self.event_subscription,
            offering_uuid=offering_uuid,
            object_type="resource",
        )
        vhost = self.event_subscription.user.uuid.hex

        orphan_name = "subscription_deadbeef_offering_cafebabe_order"

        mock_task_backend.list_all_subscription_queues.return_value = [
            {
                "vhost": vhost,
                "queues": [
                    {"name": queue.queue_name},
                    {"name": orphan_name},
                ],
                "total_messages": 3,
            }
        ]
        mock_task_backend.delete_queue.return_value = True

        tasks.cleanup_orphan_subscription_queues()

        mock_task_backend.delete_queue.assert_called_once_with(vhost, orphan_name)

    @patch("waldur_core.logging.tasks.backend.RabbitMQManagementBackend")
    def test_no_queues_found_no_action(self, mock_task_backend_class):
        mock_task_backend = mock_task_backend_class.return_value
        mock_task_backend.list_all_subscription_queues.return_value = []

        tasks.cleanup_orphan_subscription_queues()

        mock_task_backend.delete_queue.assert_not_called()

    @patch("waldur_core.logging.tasks.backend.RabbitMQManagementBackend")
    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_delete_failure_does_not_stop_processing(
        self, mock_handler_backend_class, mock_task_backend_class
    ):
        mock_task_backend = mock_task_backend_class.return_value
        vhost = self.event_subscription.user.uuid.hex

        mock_task_backend.list_all_subscription_queues.return_value = [
            {
                "vhost": vhost,
                "queues": [
                    {"name": "subscription_orphan1_offering_aaa_resource"},
                    {"name": "subscription_orphan2_offering_bbb_order"},
                ],
                "total_messages": 0,
            }
        ]
        # First call fails, second succeeds
        mock_task_backend.delete_queue.side_effect = [
            Exception("timeout"),
            True,
        ]

        # Should not raise
        tasks.cleanup_orphan_subscription_queues()

        self.assertEqual(mock_task_backend.delete_queue.call_count, 2)
