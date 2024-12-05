from datetime import timedelta
from unittest.mock import patch

from rest_framework.test import APITransactionTestCase

from waldur_core.logging import models, tasks
from waldur_core.logging.tests import factories


class DeleteStaleEventSubscriptionsTest(APITransactionTestCase):
    @patch("waldur_core.logging.backend.RabbitMQManagementBackend.delete_rabbitmq_user")
    def test_delete_stale_event_subscriptions(self, mock_delete_rabbitmq_user):
        event_subscription1 = factories.EventSubscriptionFactory()
        event_subscription2 = factories.EventSubscriptionFactory()

        user1 = event_subscription1.user
        user1.token_lifetime = 1
        user1.save()
        token1 = user1.auth_token
        token1.created = token1.created - timedelta(days=1)
        token1.save()

        user2 = event_subscription2.user
        user2.token_lifetime = 60 * 60 * 24 * 100  # 100 days
        user2.save()
        token2 = user2.auth_token
        token2.created = token2.created - timedelta(days=1)
        token2.save()

        tasks.delete_stale_event_subscriptions()

        mock_delete_rabbitmq_user.assert_called_once()
        self.assertEqual(models.EventSubscription.objects.count(), 1)
        self.assertEqual(models.EventSubscription.objects.first(), event_subscription2)
