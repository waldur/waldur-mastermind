"""Staff/support-only registration of a global EventConsumer queue."""

from unittest import mock

from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories

URL = "/api/event-consumers/register/"


@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.create_queue")
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
)
class EventConsumerRegistrationTest(test.APITestCase):
    def test_staff_can_register_a_global_consumer(self, *mocks):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        response = self.client.post(URL, {"object_types": ["user_role"]}, format="json")

        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.content
        )
        consumer = logging_models.EventConsumer.objects.get(user=staff)
        self.assertFalse(consumer.scopes.exists())  # global
        self.assertTrue(consumer.queue_created)
        self.assertTrue(consumer.rmq_username)
        self.assertEqual(response.json()["queue_name"], consumer.queue_name)
        self.assertEqual(response.json()["vhost"], staff.uuid.hex)

    def test_support_can_register(self, *mocks):
        support = structure_factories.UserFactory()
        support.is_support = True
        support.save()
        self.client.force_authenticate(support)

        response = self.client.post(URL, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_regular_user_is_forbidden(self, *mocks):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user)

        response = self.client.post(URL, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            logging_models.EventConsumer.objects.filter(user=user).exists()
        )

    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.list_rabbitmq_vhost_permissions"
    )
    @mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.get_user")
    def test_register_is_idempotent(
        self,
        mock_get_user,
        mock_vhost_perms,
        mock_vhost,
        mock_user,
        mock_perms,
        mock_queue,
    ):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        first = self.client.post(URL, {}, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        consumer = logging_models.EventConsumer.objects.get(user=staff)

        # Fast path requires BOTH the RMQ user to exist AND the vhost to still
        # grant it permission (see the vhost-permission check in register).
        mock_get_user.return_value = {"name": consumer.rmq_username}
        mock_vhost_perms.return_value = [consumer.rmq_username]
        second = self.client.post(URL, {}, format="json")
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(second.json()["rmq_username"], first.json()["rmq_username"])
        # Still exactly one global consumer for this user.
        self.assertEqual(
            logging_models.EventConsumer.objects.filter(user=staff).count(), 1
        )
