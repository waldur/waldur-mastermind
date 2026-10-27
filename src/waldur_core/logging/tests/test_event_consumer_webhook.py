"""Webhook delivery for EventConsumer: dispatch, payload filters, HTTP delivery,
the staff-only API, and resync of the current role state."""

import datetime
import http.server
import ipaddress
import json
import os
import ssl
import tempfile
import threading
from unittest import mock

import certifi
import requests
from celery.exceptions import Retry, SoftTimeLimitExceeded
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework import status, test

from waldur_core.logging import event_dispatch, tasks, utils
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import ObservableObjectType
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories

DELAY = "waldur_core.logging.tasks.publish_messages.delay"
POST = "waldur_core.logging.utils.requests.post"

# An IP literal resolves without DNS, so validate_outbound_url accepts it
# offline. The long signed query string is the case URLField rejected.
LISTENER_URL = "https://8.8.8.8/listener?scenario=1&sig=" + "a" * 250
PRIVATE_URL = "https://10.0.0.1/listener"


def _messages(mock_delay):
    msgs = []
    for call in mock_delay.call_args_list:
        msgs.extend(call.args[0])
    return msgs


def _webhook_consumer(user, **kwargs):
    return logging_factories.EventConsumerFactory(
        user=user,
        delivery=logging_models.EventConsumer.Delivery.WEBHOOK,
        webhook_url=LISTENER_URL,
        **kwargs,
    )


def _response(status_code):
    response = mock.Mock()
    response.status_code = status_code
    return response


class WebhookDispatchTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.consumer = _webhook_consumer(self.staff, object_types=["user_role"])
        self.customer = structure_factories.CustomerFactory(
            registration_code="70000001"
        )
        self.user = structure_factories.UserFactory(email="owner@example.com")

    @mock.patch(DELAY)
    def test_role_grant_is_routed_to_webhook_consumer(self, mock_delay):
        self.customer.add_user(self.user, CustomerRole.OWNER)

        msgs = _messages(mock_delay)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["webhook_consumer"], self.consumer.uuid.hex)
        self.assertNotIn("topic", msgs[0])
        payload = json.loads(msgs[0]["payload"])
        self.assertEqual(payload["event_type"], "role_granted")
        self.assertEqual(payload["role_name"], "CUSTOMER.OWNER")
        self.assertEqual(payload["email"], "owner@example.com")
        self.assertTrue(payload["granted"])
        self.assertEqual(payload["customer_uuid"], self.customer.uuid.hex)
        self.assertEqual(payload["customer_name"], self.customer.name)
        self.assertEqual(payload["customer_registration_code"], "70000001")

    @mock.patch(DELAY)
    def test_messages_carry_emission_time(self, mock_delay):
        self.customer.add_user(self.user, CustomerRole.OWNER)
        payload = json.loads(_messages(mock_delay)[0]["payload"])
        self.assertIn("emitted_at", payload)

    @mock.patch(DELAY)
    def test_filters_compare_types_strictly(self, mock_delay):
        # True == 1 in Python; a filter on 1 must not match granted=true.
        self.consumer.payload_filters = {"granted": [1]}
        self.consumer.save()
        self.customer.add_user(self.user, CustomerRole.OWNER)
        self.assertEqual(_messages(mock_delay), [])

    def test_webhook_delivery_does_not_suppress_legacy_queues(self):
        # The marketplace path skips a user's legacy subscriptions for the
        # users in user_ids; a webhook is someone else's system, so its owner
        # must not be counted as covered.
        result = event_dispatch.build_messages(
            [], lambda: {"x": 1}, ObservableObjectType.USER_ROLE, include_global=True
        )
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.user_ids, set())

    @mock.patch(DELAY)
    def test_project_role_carries_parent_organization(self, mock_delay):
        project = structure_factories.ProjectFactory(customer=self.customer)
        project.add_user(self.user, ProjectRole.MEMBER)

        payload = json.loads(_messages(mock_delay)[0]["payload"])
        self.assertEqual(payload["scope_type"], "project")
        self.assertEqual(payload["customer_uuid"], self.customer.uuid.hex)
        self.assertEqual(payload["customer_registration_code"], "70000001")

    @mock.patch(DELAY)
    def test_payload_filters_restrict_delivery(self, mock_delay):
        self.consumer.payload_filters = {"role_name": ["CUSTOMER.OWNER"]}
        self.consumer.save()
        project = structure_factories.ProjectFactory(customer=self.customer)

        project.add_user(self.user, ProjectRole.MEMBER)
        self.assertEqual(_messages(mock_delay), [])

        self.customer.add_user(self.user, CustomerRole.OWNER)
        self.assertEqual(len(_messages(mock_delay)), 1)

    @mock.patch(DELAY)
    def test_role_revocation_is_delivered(self, mock_delay):
        self.customer.add_user(self.user, CustomerRole.OWNER)
        mock_delay.reset_mock()

        self.customer.remove_user(self.user, CustomerRole.OWNER)

        payload = json.loads(_messages(mock_delay)[0]["payload"])
        self.assertEqual(payload["event_type"], "role_revoked")
        self.assertFalse(payload["granted"])

    @mock.patch(DELAY)
    def test_queue_consumer_honours_payload_filters(self, mock_delay):
        self.consumer.delete()
        logging_factories.EventConsumerFactory(
            user=self.staff,
            queue_created=True,
            rmq_username="aaaa0000000000000000000000000001",
            payload_filters={"role_name": ["CUSTOMER.OWNER"]},
        )
        project = structure_factories.ProjectFactory(customer=self.customer)

        project.add_user(self.user, ProjectRole.MEMBER)
        self.assertEqual(_messages(mock_delay), [])

        self.customer.add_user(self.user, CustomerRole.OWNER)
        self.assertIn("topic", _messages(mock_delay)[0])

    @mock.patch(DELAY)
    def test_demoted_owner_stops_receiving(self, mock_delay):
        self.staff.is_staff = False
        self.staff.save()
        mock_delay.reset_mock()

        self.customer.add_user(self.user, CustomerRole.OWNER)
        self.assertEqual(_messages(mock_delay), [])

    @mock.patch(DELAY)
    def test_consumer_without_url_receives_nothing(self, mock_delay):
        logging_models.EventConsumer.objects.filter(pk=self.consumer.pk).update(
            webhook_url=""
        )
        self.customer.add_user(self.user, CustomerRole.OWNER)
        self.assertEqual(_messages(mock_delay), [])


class PublishRoutingTest(TestCase):
    @mock.patch("waldur_core.logging.tasks.deliver_webhook_messages.delay")
    @mock.patch(DELAY)
    def test_mixed_batch_is_split_by_transport(self, mock_publish, mock_deliver):
        stomp = {"vhost": "v", "topic": "consumer_x", "payload": "{}"}
        messages = [
            {"webhook_consumer": "c1", "payload": "1"},
            stomp,
            {"webhook_consumer": "c2", "payload": "2"},
            {"webhook_consumer": "c1", "payload": "3"},
        ]

        tasks.publish_messages(messages)

        mock_deliver.assert_has_calls(
            [mock.call("c1", ["1", "3"]), mock.call("c2", ["2"])], any_order=True
        )
        mock_publish.assert_called_once_with([stomp])

    @mock.patch("waldur_core.logging.utils.publish_stomp_messages")
    @mock.patch("waldur_core.logging.tasks.deliver_webhook_messages.delay")
    def test_stomp_only_batch_is_published_directly(self, mock_deliver, mock_stomp):
        mock_stomp.return_value = (1, 0)
        stomp = {"vhost": "v", "topic": "consumer_x", "payload": "{}"}

        tasks.publish_messages([stomp])

        mock_stomp.assert_called_once_with([stomp])
        mock_deliver.assert_not_called()


class WebhookDeliveryTaskTest(TestCase):
    def setUp(self):
        self.consumer = _webhook_consumer(
            structure_factories.UserFactory(is_staff=True)
        )

    @mock.patch(POST)
    def test_posts_payloads_in_order(self, mock_post):
        mock_post.return_value = _response(200)

        result = tasks.deliver_webhook_messages(
            self.consumer.uuid.hex, ['{"n": 1}', '{"n": 2}']
        )

        self.assertEqual(result, {"sent": 2, "dropped": 0})
        bodies = [call.kwargs["data"] for call in mock_post.call_args_list]
        self.assertEqual(bodies, [b'{"n": 1}', b'{"n": 2}'])
        call = mock_post.call_args_list[0]
        self.assertEqual(call.args[0], LISTENER_URL)
        self.assertEqual(call.kwargs["headers"]["Content-Type"], "application/json")
        self.assertFalse(call.kwargs["allow_redirects"])
        self.consumer.refresh_from_db()
        self.assertIsNotNone(self.consumer.last_delivered_at)

    @mock.patch.object(tasks.deliver_webhook_messages, "retry", side_effect=Retry)
    @mock.patch(POST)
    def test_failure_retries_with_undelivered_remainder(self, mock_post, mock_retry):
        mock_post.side_effect = [_response(200), _response(503)]

        with self.assertRaises(Retry):
            tasks.deliver_webhook_messages(self.consumer.uuid.hex, ["1", "2", "3"])

        self.assertEqual(
            mock_retry.call_args.kwargs["args"], [self.consumer.uuid.hex, ["2", "3"]]
        )
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.last_error, "Listener answered HTTP 503.")
        self.assertIsNotNone(self.consumer.last_error_at)
        self.assertIsNotNone(self.consumer.last_delivered_at)

    @mock.patch.object(tasks.deliver_webhook_messages, "retry", side_effect=Retry)
    @mock.patch(POST)
    def test_error_text_never_contains_the_url(self, mock_post, mock_retry):
        mock_post.side_effect = requests.ConnectionError(
            f"Max retries exceeded with url: {LISTENER_URL}"
        )

        with self.assertRaises(Retry):
            tasks.deliver_webhook_messages(self.consumer.uuid.hex, ["1"])

        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.last_error, "Request failed: ConnectionError.")
        self.assertNotIn("sig=", self.consumer.last_error)

    @mock.patch(POST)
    def test_certificate_is_verified_by_default(self, mock_post):
        mock_post.return_value = _response(200)
        tasks.deliver_webhook_messages(self.consumer.uuid.hex, ["1"])
        self.assertIs(mock_post.call_args.kwargs["verify"], True)

    @override_settings(VERIFY_WEBHOOK_REQUESTS=False)
    @mock.patch(POST)
    def test_deployment_can_turn_verification_off(self, mock_post):
        mock_post.return_value = _response(200)
        tasks.deliver_webhook_messages(self.consumer.uuid.hex, ["1"])
        self.assertIs(mock_post.call_args.kwargs["verify"], False)

    @mock.patch.object(tasks.deliver_webhook_messages, "retry", side_effect=Retry)
    @mock.patch(POST)
    def test_untrusted_certificate_is_named_without_the_url(
        self, mock_post, mock_retry
    ):
        mock_post.side_effect = requests.exceptions.SSLError(
            f"Max retries exceeded with url: {LISTENER_URL} (Caused by "
            "SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] "
            "certificate verify failed: self-signed certificate')))"
        )

        with self.assertRaises(Retry):
            tasks.deliver_webhook_messages(self.consumer.uuid.hex, ["1"])

        self.consumer.refresh_from_db()
        self.assertEqual(
            self.consumer.last_error, "TLS certificate verification failed."
        )

    @mock.patch.object(tasks.deliver_webhook_messages, "retry", side_effect=Retry)
    @mock.patch(POST)
    def test_other_tls_failure_is_a_handshake_failure(self, mock_post, mock_retry):
        mock_post.side_effect = requests.exceptions.SSLError(
            f"url: {LISTENER_URL} (Caused by SSLError('wrong version number'))"
        )

        with self.assertRaises(Retry):
            tasks.deliver_webhook_messages(self.consumer.uuid.hex, ["1"])

        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.last_error, "TLS handshake failed.")

    @mock.patch.object(tasks.deliver_webhook_messages, "retry", side_effect=Retry)
    @mock.patch(POST)
    def test_destination_turned_private_is_not_contacted(self, mock_post, mock_retry):
        logging_models.EventConsumer.objects.filter(pk=self.consumer.pk).update(
            webhook_url=PRIVATE_URL
        )

        with self.assertRaises(Retry):
            tasks.deliver_webhook_messages(self.consumer.uuid.hex, ["1"])

        mock_post.assert_not_called()

    @mock.patch.object(tasks.deliver_webhook_messages, "retry", side_effect=Retry)
    @mock.patch(POST)
    def test_retry_resends_the_same_message_id(self, mock_post, mock_retry):
        mock_post.return_value = _response(500)
        for _ in range(2):
            with self.assertRaises(Retry):
                tasks.deliver_webhook_messages(self.consumer.uuid.hex, ['{"n": 1}'])
        ids = [
            c.kwargs["headers"]["X-Waldur-Message-Id"] for c in mock_post.call_args_list
        ]
        self.assertEqual(len(set(ids)), 1)

    @mock.patch(POST)
    def test_retries_exhausted_drops_remainder(self, mock_post):
        mock_post.return_value = _response(500)
        task = tasks.deliver_webhook_messages
        with mock.patch.object(
            type(task.request), "retries", tasks.WEBHOOK_MAX_RETRIES, create=True
        ):
            result = task(self.consumer.uuid.hex, ["1", "2"])
        self.assertEqual(result, {"sent": 0, "dropped": 2})

    @mock.patch("waldur_core.logging.tasks.deliver_webhook_messages.delay")
    @mock.patch(POST)
    def test_soft_time_limit_requeues_remainder(self, mock_post, mock_delay):
        mock_post.side_effect = [_response(200), SoftTimeLimitExceeded()]
        result = tasks.deliver_webhook_messages(self.consumer.uuid.hex, ["1", "2", "3"])
        self.assertEqual(result, {"sent": 1, "requeued": 2})
        mock_delay.assert_called_once_with(self.consumer.uuid.hex, ["2", "3"])

    @mock.patch(POST)
    def test_demoted_owner_is_not_delivered_to(self, mock_post):
        self.consumer.user.is_staff = False
        self.consumer.user.save()
        result = tasks.deliver_webhook_messages(self.consumer.uuid.hex, ["1"])
        self.assertEqual(result, {"sent": 0, "dropped": 1})
        mock_post.assert_not_called()

    @mock.patch(POST)
    def test_response_body_is_never_read(self, mock_post):
        response = _response(200)
        mock_post.return_value = response
        tasks.deliver_webhook_messages(self.consumer.uuid.hex, ["1"])
        self.assertTrue(mock_post.call_args.kwargs["stream"])
        response.close.assert_called_once()

    @mock.patch(POST)
    def test_deleted_consumer_drops_payloads(self, mock_post):
        consumer_uuid = self.consumer.uuid.hex
        self.consumer.delete()

        result = tasks.deliver_webhook_messages(consumer_uuid, ["1"])

        self.assertEqual(result, {"sent": 0, "dropped": 1})
        mock_post.assert_not_called()


class WebhookConsumerApiTest(test.APITestCase):
    URL = "/api/event-consumers/register_webhook/"

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)

    def _create(self, user, **data):
        self.client.force_authenticate(user)
        body = {"webhook_url": LISTENER_URL, **data}
        return self.client.post(self.URL, body, format="json")

    def test_staff_creates_global_webhook_consumer(self):
        response = self._create(
            self.staff,
            object_types=["user_role"],
            payload_filters={"role_name": ["CUSTOMER.OWNER"]},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        consumer = logging_models.EventConsumer.objects.get(user=self.staff)
        self.assertTrue(consumer.is_webhook)
        self.assertTrue(consumer.is_global)
        self.assertEqual(consumer.webhook_url, LISTENER_URL)
        self.assertFalse(consumer.queue_created)
        self.assertEqual(consumer.authorized_via, "staff")
        self.assertEqual(response.data["delivery"], "webhook")
        self.assertEqual(response.data["webhook_destination"], "https://8.8.8.8/…")
        self.assertNotIn("sig=", response.content.decode())
        self.assertIsNone(response.data["delivery_blocked_reason"])

    def test_support_cannot_create(self):
        support = structure_factories.UserFactory(is_support=True)
        self.assertEqual(self._create(support).status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_create(self):
        user = structure_factories.UserFactory()
        self.assertEqual(self._create(user).status_code, status.HTTP_403_FORBIDDEN)

    def test_private_destination_is_rejected(self):
        response = self._create(self.staff, webhook_url=PRIVATE_URL)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("webhook_url", response.data)

    def test_plain_http_is_rejected(self):
        response = self._create(self.staff, webhook_url="http://8.8.8.8/hook")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("webhook_url", response.data)

    def test_userinfo_is_rejected(self):
        response = self._create(
            self.staff, webhook_url="https://svc:s3cret@8.8.8.8/hook"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("webhook_url", response.data)

    def test_destination_never_shows_userinfo_or_path(self):
        # A row stored before validation existed, or written directly.
        _webhook_consumer(self.staff)
        logging_models.EventConsumer.objects.update(
            webhook_url="https://svc:s3cret@8.8.8.8:8443/hook?sig=x"
        )
        self.client.force_authenticate(self.staff)

        response = self.client.get("/api/event-consumers/")

        self.assertEqual(
            response.data[0]["webhook_destination"], "https://8.8.8.8:8443/…"
        )
        self.assertNotIn("s3cret", response.content.decode())

    def test_invalid_port_is_rejected(self):
        for url in ("https://8.8.8.8:99999/hook", "https://8.8.8.8:abc/hook"):
            response = self._create(self.staff, webhook_url=url)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, url)
            self.assertIn("webhook_url", response.data)

    def test_listing_survives_a_stored_invalid_port(self):
        _webhook_consumer(self.staff)
        logging_models.EventConsumer.objects.update(
            webhook_url="https://8.8.8.8:99999/hook"
        )
        self.client.force_authenticate(self.staff)

        response = self.client.get("/api/event-consumers/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["webhook_destination"], "https://8.8.8.8/…")

    def test_null_filter_values_are_rejected(self):
        response = self._create(self.staff, payload_filters={"role_name": [None]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_editing_staff_takes_ownership_and_is_audited(self):
        consumer = _webhook_consumer(self.staff)
        other_staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(other_staff)

        response = self.client.post(
            f"/api/event-consumers/{consumer.uuid.hex}/update_webhook/",
            {"webhook_url": "https://8.8.4.4/other"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        consumer.refresh_from_db()
        self.assertEqual(consumer.user, other_staff)
        event = logging_models.Event.objects.get(
            event_type="event_consumer_webhook_updated"
        )
        self.assertEqual(event.context["webhook_destination"], "https://8.8.4.4/…")
        self.assertNotIn("other", json.dumps(event.context))

    def test_support_cannot_deregister_webhook(self):
        consumer = _webhook_consumer(self.staff)
        support = structure_factories.UserFactory(is_support=True)
        self.client.force_authenticate(support)

        response = self.client.delete(f"/api/event-consumers/{consumer.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            logging_models.EventConsumer.objects.filter(pk=consumer.pk).exists()
        )

    def test_staff_can_deregister_webhook(self):
        consumer = _webhook_consumer(self.staff)
        self.client.force_authenticate(self.staff)

        response = self.client.delete(f"/api/event-consumers/{consumer.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_malformed_payload_filters_are_rejected(self):
        response = self._create(self.staff, payload_filters={"role_name": "x"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("payload_filters", response.data)

    def test_url_is_never_listed(self):
        _webhook_consumer(self.staff)
        self.client.force_authenticate(self.staff)

        response = self.client.get("/api/event-consumers/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("sig=", response.content.decode())

    def test_filter_by_delivery(self):
        _webhook_consumer(self.staff)
        logging_factories.EventConsumerFactory(user=self.staff)
        self.client.force_authenticate(self.staff)

        response = self.client.get("/api/event-consumers/", {"delivery": "webhook"})

        self.assertEqual([c["delivery"] for c in response.data], ["webhook"])

    def test_staff_updates_filters_and_url(self):
        consumer = _webhook_consumer(self.staff)
        self.client.force_authenticate(self.staff)
        new_url = "https://8.8.4.4/other"

        response = self.client.post(
            f"/api/event-consumers/{consumer.uuid.hex}/update_webhook/",
            {"webhook_url": new_url, "payload_filters": {"scope_type": ["customer"]}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        consumer.refresh_from_db()
        self.assertEqual(consumer.webhook_url, new_url)
        self.assertEqual(consumer.payload_filters, {"scope_type": ["customer"]})

    def test_update_of_queue_consumer_is_rejected(self):
        consumer = logging_factories.EventConsumerFactory(user=self.staff)
        self.client.force_authenticate(self.staff)

        response = self.client.post(
            f"/api/event-consumers/{consumer.uuid.hex}/update_webhook/",
            {"payload_filters": {}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


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
class QueueRegistrationIgnoresWebhookConsumerTest(test.APITestCase):
    def test_global_queue_is_not_provisioned_onto_global_webhook(self, *mocks):
        staff = structure_factories.UserFactory(is_staff=True)
        webhook = _webhook_consumer(staff)
        self.client.force_authenticate(staff)

        response = self.client.post("/api/event-consumers/register/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            logging_models.EventConsumer.objects.filter(user=staff).count(), 2
        )
        webhook.refresh_from_db()
        self.assertFalse(webhook.queue_created)
        self.assertEqual(webhook.rmq_username, "")


class ResyncTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.customer = structure_factories.CustomerFactory(
            registration_code="70000001"
        )
        self.owner = structure_factories.UserFactory()
        self.member = structure_factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        project = structure_factories.ProjectFactory(customer=self.customer)
        project.add_user(self.member, ProjectRole.MEMBER)
        self.consumer = _webhook_consumer(
            self.staff,
            object_types=["user_role"],
            payload_filters={"role_name": ["CUSTOMER.OWNER"]},
        )

    def _url(self, consumer):
        return f"/api/event-consumers/{consumer.uuid.hex}/resync/"

    @mock.patch("waldur_core.logging.event_dispatch.resync_event_consumer.delay")
    def test_staff_queues_resync(self, mock_resync):
        self.client.force_authenticate(self.staff)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self._url(self.consumer))

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["object_types"], ["user_role"])
        mock_resync.assert_called_once_with(self.consumer.uuid.hex)

    @mock.patch("waldur_core.logging.event_dispatch.resync_event_consumer.delay")
    def test_resync_is_throttled_and_audited(self, mock_resync):
        self.client.force_authenticate(self.staff)

        first = self.client.post(self._url(self.consumer))
        second = self.client.post(self._url(self.consumer))

        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(
            logging_models.Event.objects.filter(
                event_type="event_consumer_resync_requested"
            ).count(),
            1,
        )

    def test_filters_that_exclude_snapshots_are_rejected(self):
        self.consumer.payload_filters = {"event_type": ["role_granted"]}
        self.consumer.save()
        self.client.force_authenticate(self.staff)

        response = self.client.post(self._url(self.consumer))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch(DELAY)
    def test_resync_scope_type_filter(self, mock_delay):
        self.consumer.payload_filters = {"scope_type": ["project"]}
        self.consumer.save()

        result = event_dispatch.resync_event_consumer(self.consumer.uuid.hex)

        payloads = [json.loads(m["payload"]) for m in _messages(mock_delay)]
        self.assertEqual(result, {"sent": len(payloads)})
        self.assertEqual({p["user_uuid"] for p in payloads}, {self.member.uuid.hex})
        self.assertEqual({p["scope_type"] for p in payloads}, {"project"})

    def test_regular_user_cannot_resync(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.post(self._url(self.consumer))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_bound_consumer_cannot_resync(self):
        bound = logging_factories.EventConsumerFactory.with_scopes(
            self.customer,
            user=self.staff,
            delivery=logging_models.EventConsumer.Delivery.WEBHOOK,
            webhook_url=LISTENER_URL,
        )
        self.client.force_authenticate(self.staff)

        response = self.client.post(self._url(bound))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_consumer_without_role_messages_cannot_resync(self):
        self.consumer.object_types = ["user_profile"]
        self.consumer.save()
        self.client.force_authenticate(self.staff)

        response = self.client.post(self._url(self.consumer))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch(DELAY)
    def test_resync_sends_filtered_snapshot(self, mock_delay):
        result = event_dispatch.resync_event_consumer(self.consumer.uuid.hex)

        msgs = _messages(mock_delay)
        self.assertEqual(result, {"sent": 1})
        self.assertEqual(msgs[0]["webhook_consumer"], self.consumer.uuid.hex)
        payload = json.loads(msgs[0]["payload"])
        self.assertEqual(payload["event_type"], "role_snapshot")
        self.assertEqual(payload["object_type"], "user_role")
        self.assertTrue(payload["granted"])
        self.assertEqual(payload["user_uuid"], self.owner.uuid.hex)
        self.assertEqual(payload["customer_registration_code"], "70000001")

    @mock.patch(DELAY)
    def test_resync_skips_revoked_grants_and_inactive_users(self, mock_delay):
        self.customer.remove_user(self.owner, CustomerRole.OWNER)
        inactive = structure_factories.UserFactory()
        self.customer.add_user(inactive, CustomerRole.OWNER)
        inactive.is_active = False
        inactive.save()
        mock_delay.reset_mock()

        result = event_dispatch.resync_event_consumer(self.consumer.uuid.hex)

        self.assertEqual(result, {"sent": 0})

    @mock.patch(DELAY)
    def test_resync_for_demoted_owner_sends_nothing(self, mock_delay):
        self.staff.is_staff = False
        self.staff.save()
        mock_delay.reset_mock()

        result = event_dispatch.resync_event_consumer(self.consumer.uuid.hex)

        self.assertEqual(result, {"sent": 0})
        mock_delay.assert_not_called()

    @mock.patch(DELAY)
    def test_resync_batches_large_snapshots(self, mock_delay):
        self.consumer.payload_filters = {}
        self.consumer.save()
        with mock.patch.object(event_dispatch, "SNAPSHOT_BATCH_SIZE", 1):
            result = event_dispatch.resync_event_consumer(self.consumer.uuid.hex)

        self.assertEqual(result["sent"], mock_delay.call_count)
        self.assertGreaterEqual(mock_delay.call_count, 2)


def _private_pki(ip="127.0.0.1"):
    """A private CA, and a listener certificate and key for ``ip`` it issued, as PEM."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Private CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    listener = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return (
        ca.public_bytes(serialization.Encoding.PEM).decode(),
        listener.public_bytes(serialization.Encoding.PEM).decode(),
        key_pem.decode(),
    )


class _NoContentHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


class DeploymentTrustBundleTest(TestCase):
    """A listener with a private CA, trusted through REQUESTS_CA_BUNDLE as the
    deployment guide describes: a real TLS handshake, not a mock."""

    def setUp(self):
        ca_pem, listener_pem, key_pem = _private_pki()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        def write(name, content):
            path = os.path.join(directory.name, name)
            with open(path, "w") as fh:
                fh.write(content)
            return path

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            write("listener.pem", listener_pem), write("listener.key", key_pem)
        )
        with open(certifi.where()) as fh:
            public_roots = fh.read()
        self.bundle = write("ca-bundle.pem", public_roots + ca_pem)

        server = http.server.HTTPServer(("127.0.0.1", 0), _NoContentHandler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"https://127.0.0.1:{server.server_address[1]}/"

        # The guard would refuse a loopback listener; it is not what is tested.
        patcher = mock.patch.object(utils, "validate_outbound_url")
        patcher.start()
        self.addCleanup(patcher.stop)
        # Straight to the listener, even where the runner sets a proxy.
        env = mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"})
        env.start()
        self.addCleanup(env.stop)

    def _post(self):
        utils.post_consumer_webhook(self.url, "{}", "id")

    def test_private_ca_is_not_trusted_by_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("REQUESTS_CA_BUNDLE", None)
            os.environ.pop("CURL_CA_BUNDLE", None)
            with self.assertRaisesMessage(
                utils.WebhookDeliveryError, "TLS certificate verification failed."
            ):
                self._post()

    def test_private_ca_in_the_deployment_bundle_is_trusted(self):
        with mock.patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": self.bundle}):
            self._post()
