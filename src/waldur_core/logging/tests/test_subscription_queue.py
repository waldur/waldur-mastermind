import uuid
from unittest.mock import patch

from rest_framework import status, test

from waldur_core.logging import models, tasks
from waldur_core.logging.tests import factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures


class EventSubscriptionQueueModelTest(test.APITestCase):
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


class EventSubscriptionQueueSerializerTest(test.APITestCase):
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


class EventSubscriptionQueueDeleteSignalTest(test.APITestCase):
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


class CleanupOrphanSubscriptionQueuesTest(test.APITestCase):
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


class EventSubscriptionCreateQueueActionTest(test.APITestCase):
    """Tests for the create_queue action on EventSubscriptionViewSet."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.event_subscription = factories.EventSubscriptionFactory(
            user=self.fixture.owner,
            observable_objects=[{"object_type": "resource"}],
        )
        self.url = factories.EventSubscriptionFactory.get_url(
            self.event_subscription, action="create_queue"
        )

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_create_queue_success(self, mock_backend_class):
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        mock_backend = mock_backend_class.return_value
        mock_backend.create_queue.return_value = True

        # Create an offering the user has access to
        offering = marketplace_factories.OfferingFactory(customer=self.fixture.customer)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 1)

        queue = models.EventSubscriptionQueue.objects.first()
        self.assertEqual(queue.event_subscription, self.event_subscription)
        self.assertEqual(queue.offering_uuid, offering.uuid)
        self.assertEqual(queue.object_type, "resource")

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_create_queue_idempotent_returns_200(self, mock_backend_class):
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        mock_backend = mock_backend_class.return_value
        mock_backend.create_queue.return_value = True

        offering = marketplace_factories.OfferingFactory(customer=self.fixture.customer)

        # Create queue first time
        self.client.force_authenticate(self.fixture.owner)
        response1 = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        # Create same queue again - should return 200
        response2 = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 1)

    def test_create_queue_unauthorized(self):
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        offering = marketplace_factories.OfferingFactory()

        # Not authenticated
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_create_queue_wrong_user_cannot_access(self, mock_backend_class):
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        other_user = self.fixture.admin
        offering = marketplace_factories.OfferingFactory(customer=self.fixture.customer)

        # Authenticate as different user
        self.client.force_authenticate(other_user)
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )
        # Should get 404 because queryset is filtered by user
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_queue_invalid_object_type(self):
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        offering = marketplace_factories.OfferingFactory(customer=self.fixture.customer)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "invalid_type",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("object_type", response.data)

    def test_create_queue_nonexistent_offering(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(uuid.uuid4()),
                "object_type": "resource",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_uuid", response.data)

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_create_queue_no_access_to_offering(self, mock_backend_class):
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        # Create offering in different customer
        other_customer = fixtures.CustomerFixture().customer
        offering = marketplace_factories.OfferingFactory(customer=other_customer)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_uuid", response.data)


class ISDManagerEventSubscriptionQueueTest(test.APITestCase):
    """Tests for ISD identity manager access to event subscription queue creation."""

    def setUp(self):
        from waldur_mastermind.marketplace import enums as marketplace_enums
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        self.marketplace_enums = marketplace_enums
        self.marketplace_factories = marketplace_factories

        self.isd_manager = structure_factories.UserFactory(
            is_identity_manager=True,
            managed_isds=["isd:efp"],
        )
        self.event_subscription = factories.EventSubscriptionFactory(
            user=self.isd_manager,
            observable_objects=[{"object_type": "resource"}],
        )
        self.url = factories.EventSubscriptionFactory.get_url(
            self.event_subscription, action="create_queue"
        )

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_isd_manager_can_create_queue_for_active_offering(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.create_queue.return_value = True

        offering = self.marketplace_factories.OfferingFactory(
            state=self.marketplace_enums.OfferingStates.ACTIVE,
        )

        self.client.force_authenticate(self.isd_manager)
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 1)

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_isd_manager_can_create_queue_for_paused_offering(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.create_queue.return_value = True

        offering = self.marketplace_factories.OfferingFactory(
            state=self.marketplace_enums.OfferingStates.PAUSED,
        )

        self.client.force_authenticate(self.isd_manager)
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_isd_manager_can_create_queue_for_unavailable_offering(
        self, mock_backend_class
    ):
        mock_backend = mock_backend_class.return_value
        mock_backend.create_queue.return_value = True

        offering = self.marketplace_factories.OfferingFactory(
            state=self.marketplace_enums.OfferingStates.UNAVAILABLE,
        )

        self.client.force_authenticate(self.isd_manager)
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_isd_manager_cannot_create_queue_for_draft_offering(self):
        offering = self.marketplace_factories.OfferingFactory(
            state=self.marketplace_enums.OfferingStates.DRAFT,
        )

        self.client.force_authenticate(self.isd_manager)
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_uuid", response.data)

    def test_isd_manager_cannot_create_queue_for_archived_offering(self):
        offering = self.marketplace_factories.OfferingFactory(
            state=self.marketplace_enums.OfferingStates.ARCHIVED,
        )

        self.client.force_authenticate(self.isd_manager)
        response = self.client.post(
            self.url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_uuid", response.data)

    def test_identity_manager_without_managed_isds_is_rejected(self):
        user = structure_factories.UserFactory(
            is_identity_manager=True,
            managed_isds=[],
        )
        subscription = factories.EventSubscriptionFactory(
            user=user,
            observable_objects=[{"object_type": "resource"}],
        )
        url = factories.EventSubscriptionFactory.get_url(
            subscription, action="create_queue"
        )
        offering = self.marketplace_factories.OfferingFactory(
            state=self.marketplace_enums.OfferingStates.ACTIVE,
        )

        self.client.force_authenticate(user)
        response = self.client.post(
            url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_uuid", response.data)

    def test_non_identity_manager_without_offering_access_is_rejected(self):
        user = structure_factories.UserFactory(
            is_identity_manager=False,
        )
        subscription = factories.EventSubscriptionFactory(
            user=user,
            observable_objects=[{"object_type": "resource"}],
        )
        url = factories.EventSubscriptionFactory.get_url(
            subscription, action="create_queue"
        )
        offering = self.marketplace_factories.OfferingFactory(
            state=self.marketplace_enums.OfferingStates.ACTIVE,
        )

        self.client.force_authenticate(user)
        response = self.client.post(
            url,
            {
                "offering_uuid": str(offering.uuid),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_uuid", response.data)


class EventSubscriptionQueueViewSetTest(test.APITestCase):
    """Tests for EventSubscriptionQueueViewSet (list, retrieve, destroy)."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.event_subscription = factories.EventSubscriptionFactory(
            user=self.fixture.owner,
            observable_objects=[{"object_type": "resource"}],
        )
        self.offering_uuid = uuid.uuid4()
        self.queue = factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=self.offering_uuid,
            object_type="resource",
        )

    def test_list_queues_for_owner(self):
        url = factories.EventSubscriptionQueueFactory.get_list_url()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.queue.uuid))

    def test_list_queues_filtered_by_user(self):
        # Create another user's subscription and queue
        other_fixture = fixtures.ProjectFixture()
        other_subscription = factories.EventSubscriptionFactory(
            user=other_fixture.owner,
            observable_objects=[{"object_type": "order"}],
        )
        factories.EventSubscriptionQueueFactory(
            event_subscription=other_subscription,
            offering_uuid=uuid.uuid4(),
            object_type="order",
        )

        url = factories.EventSubscriptionQueueFactory.get_list_url()

        # User should only see their own queues
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.queue.uuid))

    def test_list_queues_staff_sees_all(self):
        # Create another user's queue
        other_fixture = fixtures.ProjectFixture()
        other_subscription = factories.EventSubscriptionFactory(
            user=other_fixture.owner,
            observable_objects=[{"object_type": "order"}],
        )
        factories.EventSubscriptionQueueFactory(
            event_subscription=other_subscription,
            offering_uuid=uuid.uuid4(),
            object_type="order",
        )

        url = factories.EventSubscriptionQueueFactory.get_list_url()

        # Staff should see all queues
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_retrieve_queue(self):
        url = factories.EventSubscriptionQueueFactory.get_url(self.queue)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], str(self.queue.uuid))
        self.assertEqual(response.data["object_type"], "resource")

    def test_retrieve_queue_not_owned_returns_404(self):
        other_fixture = fixtures.ProjectFixture()
        other_subscription = factories.EventSubscriptionFactory(
            user=other_fixture.owner,
            observable_objects=[{"object_type": "order"}],
        )
        other_queue = factories.EventSubscriptionQueueFactory(
            event_subscription=other_subscription,
            offering_uuid=uuid.uuid4(),
            object_type="order",
        )

        url = factories.EventSubscriptionQueueFactory.get_url(other_queue)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("waldur_core.logging.backend.RabbitMQManagementBackend")
    def test_destroy_queue(self, mock_backend_class):
        mock_backend = mock_backend_class.return_value
        mock_backend.delete_queue.return_value = True

        url = factories.EventSubscriptionQueueFactory.get_url(self.queue)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 0)

    def test_destroy_queue_not_owned_returns_404(self):
        other_fixture = fixtures.ProjectFixture()
        other_subscription = factories.EventSubscriptionFactory(
            user=other_fixture.owner,
            observable_objects=[{"object_type": "order"}],
        )
        other_queue = factories.EventSubscriptionQueueFactory(
            event_subscription=other_subscription,
            offering_uuid=uuid.uuid4(),
            object_type="order",
        )

        url = factories.EventSubscriptionQueueFactory.get_url(other_queue)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(models.EventSubscriptionQueue.objects.count(), 2)

    def test_create_not_allowed(self):
        """Creating queues via POST is not allowed - must use create_queue action."""
        url = factories.EventSubscriptionQueueFactory.get_list_url()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            url,
            {
                "event_subscription": str(self.event_subscription.uuid),
                "offering_uuid": str(uuid.uuid4()),
                "object_type": "resource",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_update_not_allowed(self):
        """Updating queues is not allowed."""
        url = factories.EventSubscriptionQueueFactory.get_url(self.queue)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.put(
            url,
            {
                "object_type": "order",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_filter_by_offering_uuid(self):
        # Create another queue with different offering
        other_offering_uuid = uuid.uuid4()
        factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=other_offering_uuid,
            object_type="order",
        )

        url = factories.EventSubscriptionQueueFactory.get_list_url()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(url, {"offering_uuid": str(self.offering_uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.queue.uuid))

    def test_filter_by_object_type(self):
        # Create another queue with different object type
        factories.EventSubscriptionQueueFactory(
            event_subscription=self.event_subscription,
            offering_uuid=uuid.uuid4(),
            object_type="order",
        )

        url = factories.EventSubscriptionQueueFactory.get_list_url()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(url, {"object_type": "resource"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["object_type"], "resource")

    def test_filter_by_event_subscription_uuid(self):
        # Create another subscription and queue
        other_subscription = factories.EventSubscriptionFactory(
            user=self.fixture.owner,
            observable_objects=[{"object_type": "order"}],
        )
        factories.EventSubscriptionQueueFactory(
            event_subscription=other_subscription,
            offering_uuid=uuid.uuid4(),
            object_type="order",
        )

        url = factories.EventSubscriptionQueueFactory.get_list_url()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            url, {"event_subscription_uuid": str(self.event_subscription.uuid)}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.queue.uuid))
