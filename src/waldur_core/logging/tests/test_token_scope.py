"""Hooks, event subscriptions and event consumers of other users are reached
through the staff or support role. A staff or support user's personal access
token without the matching scope acts as the user without that role (#980).

Support is read-only: it reads other users' rows but changes only its own.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.logging import models
from waldur_core.logging.tests import factories
from waldur_core.permissions.tests.pat_utils import STAFF, SUPPORT, use_pat
from waldur_core.structure.tests import factories as structure_factories


@override_config(PAT_ENABLED=True)
class WebHookTokenScopeTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.author = structure_factories.UserFactory()
        self.hook = factories.WebHookFactory(user=self.author)
        self.url = factories.WebHookFactory.get_url(self.hook)

    def repoint(self):
        return self.client.patch(self.url, {"destination_url": "https://8.8.4.4/"})

    def assert_unchanged(self):
        self.hook.refresh_from_db()
        self.assertEqual(self.hook.destination_url, "http://example.com/")

    def test_staff_session_can_repoint(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.repoint().status_code, status.HTTP_200_OK)

    def test_staff_token_with_staff_scope_can_repoint(self):
        use_pat(self.client, self.staff, STAFF)
        self.assertEqual(self.repoint().status_code, status.HTTP_200_OK)

    def test_staff_token_without_staff_scope_cannot_repoint(self):
        use_pat(self.client, self.staff)
        self.assertEqual(self.repoint().status_code, status.HTTP_404_NOT_FOUND)
        self.assert_unchanged()

    def test_other_user_cannot_repoint(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assertEqual(self.repoint().status_code, status.HTTP_404_NOT_FOUND)
        self.assert_unchanged()

    def test_support_token_with_support_scope_still_cannot_repoint(self):
        # Support reads other users' hooks but does not change them.
        use_pat(self.client, structure_factories.UserFactory(is_support=True), SUPPORT)
        self.assertEqual(self.repoint().status_code, status.HTTP_404_NOT_FOUND)
        self.assert_unchanged()

    def test_support_token_with_support_scope_can_read(self):
        use_pat(self.client, structure_factories.UserFactory(is_support=True), SUPPORT)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

    def test_support_token_without_support_scope_cannot_read(self):
        use_pat(self.client, structure_factories.UserFactory(is_support=True))
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND
        )


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend")
class EventSubscriptionTokenScopeTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.subscription = factories.EventSubscriptionFactory()
        self.url = factories.EventSubscriptionFactory.get_url(self.subscription)

    def assert_deleted(self, response, deleted):
        if deleted:
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        else:
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            models.EventSubscription.objects.filter(pk=self.subscription.pk).exists(),
            not deleted,
        )

    def test_staff_session_can_delete(self, _backend):
        self.client.force_authenticate(self.staff)
        self.assert_deleted(self.client.delete(self.url), True)

    def test_staff_token_with_staff_scope_can_delete(self, _backend):
        use_pat(self.client, self.staff, STAFF)
        self.assert_deleted(self.client.delete(self.url), True)

    def test_staff_token_without_staff_scope_cannot_delete(self, _backend):
        use_pat(self.client, self.staff)
        self.assert_deleted(self.client.delete(self.url), False)

    def test_support_session_cannot_delete(self, _backend):
        self.client.force_authenticate(self.support)
        self.assert_deleted(self.client.delete(self.url), False)

    def test_support_session_can_read(self, _backend):
        self.client.force_authenticate(self.support)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

    def test_support_token_with_support_scope_cannot_delete(self, _backend):
        use_pat(self.client, self.support, SUPPORT)
        self.assert_deleted(self.client.delete(self.url), False)

    def test_support_session_cannot_create_queue_for_another_user(self, backend):
        self.client.force_authenticate(self.support)
        response = self.client.post(
            factories.EventSubscriptionFactory.get_url(
                self.subscription, action="create_queue"
            ),
            {"offering_uuid": "a" * 32, "object_type": "resource"},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(
            models.EventSubscriptionQueue.objects.filter(
                event_subscription=self.subscription
            ).exists()
        )

    def test_support_session_deletes_its_own(self, _backend):
        own = factories.EventSubscriptionFactory(user=self.support)
        self.client.force_authenticate(self.support)
        response = self.client.delete(factories.EventSubscriptionFactory.get_url(own))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_support_token_without_support_scope_cannot_delete(self, _backend):
        use_pat(self.client, self.support)
        self.assert_deleted(self.client.delete(self.url), False)

    def test_other_user_cannot_delete(self, _backend):
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assert_deleted(self.client.delete(self.url), False)


@override_config(PAT_ENABLED=True)
class EventConsumerTokenScopeTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.consumer = factories.EventConsumerFactory.with_scopes(
            structure_factories.CustomerFactory()
        )
        self.url = f"/api/event-consumers/{self.consumer.uuid.hex}/"

    def assert_deleted(self, response, deleted):
        if deleted:
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        else:
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            models.EventConsumer.objects.filter(pk=self.consumer.pk).exists(),
            not deleted,
        )

    def test_staff_session_can_delete(self):
        self.client.force_authenticate(self.staff)
        self.assert_deleted(self.client.delete(self.url), True)

    def test_staff_token_with_staff_scope_can_delete(self):
        use_pat(self.client, self.staff, STAFF)
        self.assert_deleted(self.client.delete(self.url), True)

    def test_staff_token_without_staff_scope_cannot_delete(self):
        use_pat(self.client, self.staff)
        self.assert_deleted(self.client.delete(self.url), False)

    def test_support_session_cannot_delete(self):
        self.client.force_authenticate(self.support)
        self.assert_deleted(self.client.delete(self.url), False)

    def test_support_session_can_list(self):
        self.client.force_authenticate(self.support)
        response = self.client.get("/api/event-consumers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(
            self.consumer.uuid.hex,
            [row["uuid"].replace("-", "") for row in response.data],
        )

    def test_support_token_with_support_scope_cannot_delete(self):
        use_pat(self.client, self.support, SUPPORT)
        self.assert_deleted(self.client.delete(self.url), False)

    def test_support_session_deletes_its_own(self):
        self.consumer.user = self.support
        self.consumer.save(update_fields=["user"])
        self.client.force_authenticate(self.support)
        self.assert_deleted(self.client.delete(self.url), True)

    def test_support_token_without_support_scope_cannot_delete(self):
        use_pat(self.client, self.support)
        self.assert_deleted(self.client.delete(self.url), False)

    def test_other_user_cannot_delete(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assert_deleted(self.client.delete(self.url), False)


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend")
class EventSubscriptionQueueSupportTest(test.APITestCase):
    def setUp(self):
        self.support = structure_factories.UserFactory(is_support=True)
        self.queue = factories.EventSubscriptionQueueFactory()
        self.url = factories.EventSubscriptionQueueFactory.get_url(self.queue)

    def queue_exists(self):
        return models.EventSubscriptionQueue.objects.filter(pk=self.queue.pk).exists()

    def test_support_session_can_read(self, _backend):
        self.client.force_authenticate(self.support)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)

    def test_support_session_cannot_delete(self, _backend):
        self.client.force_authenticate(self.support)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(self.queue_exists())

    def test_staff_session_can_delete(self, _backend):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(self.queue_exists())
