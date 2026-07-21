import json
from unittest import mock

from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.logging import enums as logging_enums
from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import enums
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent.tests import factories


def _agent_identity_url(agent_identity, action=None):
    """Build a URL for an AgentIdentity detail route (optionally an action).

    The pub/sub state moved onto the generic ``EventConsumer`` model, so the
    factory no longer exposes ``get_url``; the tests build the URL directly.
    """
    url = "http://testserver" + reverse(
        "marketplace-site-agent-identity-detail",
        kwargs={"uuid": agent_identity.uuid.hex},
    )
    return url if action is None else url + action + "/"


@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
)
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.create_queue")
class AgentQueueRegistrationTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

    def _create_agent_identity(self):
        return factories.AgentIdentityFactory(
            offering=self.offering, name="Test Agent Identity"
        )

    def _get_register_queue_url(self, agent_identity):
        return _agent_identity_url(agent_identity, action="register_queue")

    def test_register_queue_creates_rmq_resources_and_queue(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())

        agent_identity.refresh_from_db()
        consumer = agent_identity.event_consumer
        self.assertIsNotNone(consumer)
        self.assertTrue(consumer.queue_created)
        self.assertTrue(consumer.rmq_username)
        self.assertEqual(consumer.user, user)

        mock_create_vhost.assert_called_once_with(user.uuid.hex)
        mock_create_user.assert_called_once()
        mock_assign_permissions.assert_called_once()
        mock_create_queue.assert_called_once()

    def test_register_queue_without_drf_token_does_not_500(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        """A caller with no DRF Token row (e.g. PAT/SSO auth) must still be able
        to register — refresh_token get-or-creates the token instead of raising
        Token.DoesNotExist."""
        from rest_framework.authtoken.models import Token

        user = self.fixture.staff
        # force_authenticate bypasses SessionAuthentication, so (unlike
        # force_login) no token is auto-created — reproducing the token-less
        # caller the old request.user.auth_token.key would 500 on.
        Token.objects.filter(user=user).delete()
        self.client.force_authenticate(user=user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        # The password now used for the RMQ user is the get-or-created token.
        self.assertTrue(Token.objects.filter(user=user).exists())
        self.assertEqual(
            mock_create_user.call_args.args[1],
            Token.objects.get(user=user).key,
        )

    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.delete_rabbitmq_user"
    )
    def test_register_queue_cleans_up_user_when_queue_creation_fails(
        self,
        mock_delete_user,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        """If create_queue fails, the just-created RMQ user is torn down and a
        400 is returned rather than leaving a leaked user + queue_created flag.

        The EventConsumer is created before the RMQ work, so on failure it
        exists but stays unregistered (queue_created=False, rmq_username="")."""
        mock_create_queue.return_value = False

        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        mock_delete_user.assert_called_once()
        agent_identity.refresh_from_db()
        consumer = agent_identity.event_consumer
        self.assertIsNotNone(consumer)
        self.assertFalse(consumer.queue_created)
        self.assertEqual(consumer.rmq_username, "")

    def test_register_queue_does_not_create_event_subscription(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        initial_count = logging_models.EventSubscription.objects.count()
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.assertEqual(
            logging_models.EventSubscription.objects.count(), initial_count
        )

    @mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.get_user")
    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.list_rabbitmq_vhost_permissions"
    )
    def test_register_queue_idempotent(
        self,
        mock_list_permissions,
        mock_get_user,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response1 = self.client.post(url, {})
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        agent_identity.refresh_from_db()
        rmq_username = agent_identity.event_consumer.rmq_username

        mock_get_user.return_value = {"name": rmq_username}
        mock_list_permissions.return_value = [rmq_username]

        response2 = self.client.post(url, {})
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        self.assertEqual(
            response2.json()["rmq_username"],
            response1.json()["rmq_username"],
        )

    @mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.get_user")
    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.delete_rabbitmq_user"
    )
    def test_register_queue_recreates_on_stale_rmq(
        self,
        mock_delete_user,
        mock_get_user,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response1 = self.client.post(url, {})
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        agent_identity.refresh_from_db()
        first_rmq_username = agent_identity.event_consumer.rmq_username

        mock_get_user.return_value = None

        response2 = self.client.post(url, {})
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)

        self.assertNotEqual(
            response1.json()["rmq_username"], response2.json()["rmq_username"]
        )
        mock_delete_user.assert_called_once_with(first_rmq_username)

    def test_register_queue_queue_name_format(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        agent_identity.refresh_from_db()
        expected_queue_name = f"consumer_{agent_identity.event_consumer.uuid.hex}"
        self.assertEqual(response.json()["queue_name"], expected_queue_name)

    def test_register_queue_vhost_is_user_uuid(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["vhost"], user.uuid.hex)

    def test_register_queue_returns_all_object_types(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        all_types = {member.value for member in logging_enums.ObservableObjectType}
        response_types = set(response.json()["observable_object_types"])
        self.assertEqual(all_types, response_types)

    def test_register_queue_with_object_types_filter(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(
            url,
            {"object_types": ["order", "resource"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        agent_identity.refresh_from_db()
        self.assertEqual(
            agent_identity.event_consumer.object_types, ["order", "resource"]
        )
        self.assertEqual(
            set(response.json()["observable_object_types"]),
            {"order", "resource"},
        )

    def test_register_queue_empty_object_types_returns_all(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(url, {"object_types": []}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        agent_identity.refresh_from_db()
        self.assertEqual(agent_identity.event_consumer.object_types, [])

        all_types = {member.value for member in logging_enums.ObservableObjectType}
        self.assertEqual(set(response.json()["observable_object_types"]), all_types)

    def test_register_queue_invalid_object_type_rejected(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        from rest_framework.exceptions import ValidationError

        from waldur_mastermind.marketplace_site_agent.serializers import (
            AgentQueueRegistrationSerializer,
        )

        serializer = AgentQueueRegistrationSerializer(
            data={"object_types": ["order", "nonexistent_type"]}
        )
        with self.assertRaises(ValidationError):
            serializer.is_valid(raise_exception=True)


class AgentQueuePermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

    def _create_agent_identity(self):
        return factories.AgentIdentityFactory(
            offering=self.offering, name="Test Agent Identity"
        )

    def _get_register_queue_url(self, agent_identity):
        return _agent_identity_url(agent_identity, action="register_queue")

    def test_register_queue_requires_offering_permission(self):
        user = self.fixture.admin
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(url, {})
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )

    def test_update_is_gated_by_manage_permission(self):
        """Defense-in-depth: `update` (PUT) previously had no object-permission
        check, so anyone who could merely *see* the agent via get_queryset could
        modify it. It must now require the same `_can_manage_offering_agent`
        gate as register_queue/destroy. An offering manager without
        UPDATE_OFFERING can see it (filter_for_user) but must not edit it."""
        manager = self.fixture.offering_manager  # visible, but cannot manage
        agent_identity = self._create_agent_identity()
        url = _agent_identity_url(agent_identity)
        self.client.force_login(manager)

        response = self.client.put(
            url, {"offering": self.offering.uuid.hex, "name": "x"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_cannot_repoint_offering_even_for_a_manager(self):
        """The serializer guard: an actor who legitimately manages the current
        offering (here, staff) still must not move the record onto a different
        offering — the field is immutable after creation."""
        other = marketplace_fixtures.MarketplaceFixture()
        other_offering = other.offering
        other_offering.type = enums.SITE_AGENT_OFFERING
        other_offering.save()

        agent_identity = self._create_agent_identity()
        url = _agent_identity_url(agent_identity)
        self.client.force_login(self.fixture.staff)

        response = self.client.put(
            url, {"offering": other_offering.uuid.hex, "name": "x"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        agent_identity.refresh_from_db()
        self.assertEqual(agent_identity.offering_id, self.offering.id)

    def test_update_allows_same_offering_round_trip(self):
        """A full PUT that re-sends the unchanged offering still succeeds for a
        legitimate manager (staff), so agents can PUT back what they GET."""
        agent_identity = self._create_agent_identity()
        url = _agent_identity_url(agent_identity)
        self.client.force_login(self.fixture.staff)

        response = self.client.put(
            url, {"offering": self.offering.uuid.hex, "name": "renamed"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        agent_identity.refresh_from_db()
        self.assertEqual(agent_identity.name, "renamed")
        self.assertEqual(agent_identity.offering_id, self.offering.id)

    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
    )
    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
    )
    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
    )
    @mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.create_queue")
    def test_register_queue_staff_can_access(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())

    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
    )
    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
    )
    @mock.patch(
        "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
    )
    @mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.create_queue")
    def test_register_queue_offering_owner_can_access(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.offering_owner
        self.client.force_login(user)

        agent_identity = self._create_agent_identity()
        url = self._get_register_queue_url(agent_identity)

        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())


class AgentQueueMessageRoutingTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

    def _setup_consumer(self, user=None, object_types=None):
        """Create an offering-scoped EventConsumer with a registered queue.

        The pub/sub state lives on the generic EventConsumer, so routing tests
        operate directly on it (no AgentIdentity is required to route).
        """
        if user is None:
            user = self.fixture.staff

        return logging_factories.EventConsumerFactory.for_offering(
            self.offering,
            user=user,
            rmq_username="aabb000000000000000000000000ccdd",
            queue_created=True,
            object_types=object_types or [],
        )

    def test_prepare_messages_sends_to_agent_queue(self):
        user = self.fixture.staff
        consumer = self._setup_consumer(user=user)

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        consumer_topics = [
            m["topic"]
            for m in messages
            if m["topic"] == f"consumer_{consumer.uuid.hex}"
        ]
        self.assertEqual(len(consumer_topics), 1)

    def test_prepare_messages_includes_object_type_in_payload(self):
        user = self.fixture.staff
        consumer = self._setup_consumer(user=user)

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        consumer_messages = [
            m for m in messages if m["topic"] == f"consumer_{consumer.uuid.hex}"
        ]
        self.assertEqual(len(consumer_messages), 1)
        parsed_payload = json.loads(consumer_messages[0]["payload"])
        self.assertEqual(parsed_payload["object_type"], "order")

    def test_prepare_messages_old_path_unaffected_by_consumer(self):
        """A consumer queue with no EventSubscriptionQueue records should
        produce 0 messages via the old path."""
        user = self.fixture.staff
        self._setup_consumer(user=user)

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        # The old path requires EventSubscriptionQueue records to produce
        # messages. Only the consumer queue path should produce a message here.
        old_path_messages = [
            m for m in messages if m["topic"].startswith("subscription/")
        ]
        self.assertEqual(len(old_path_messages), 0)

    def test_consumer_queue_suppresses_legacy_for_same_user(self):
        """A unified consumer queue supersedes the legacy path for the SAME user:
        no double-delivery. Only the consumer message is emitted."""
        user = self.fixture.staff

        # Old path: subscription + EventSubscriptionQueue
        old_subscription = logging_models.EventSubscription.objects.create(
            user=user,
            observable_objects=[
                {"object_type": logging_enums.ObservableObjectType.ORDER.value}
            ],
        )
        logging_models.EventSubscriptionQueue.objects.create(
            event_subscription=old_subscription,
            offering_uuid=self.offering.uuid,
            object_type=logging_enums.ObservableObjectType.ORDER.value,
        )

        # New consumer path, same user
        consumer = self._setup_consumer(user=user)

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        self.assertEqual(len(messages), 1)

        old_path = [m for m in messages if m["topic"].startswith("subscription/")]
        consumer_path = [
            m for m in messages if m["topic"] == f"consumer_{consumer.uuid.hex}"
        ]
        self.assertEqual(len(old_path), 0)
        self.assertEqual(len(consumer_path), 1)

    def test_consumer_and_legacy_coexist_for_distinct_users(self):
        """Legacy subscription (user A) + consumer queue (user B) on the same
        offering both deliver: suppression is per-user, not global."""
        from waldur_core.structure.tests import factories as structure_factories

        legacy_user = self.fixture.staff
        consumer_user = structure_factories.UserFactory(is_staff=True)

        old_subscription = logging_models.EventSubscription.objects.create(
            user=legacy_user,
            observable_objects=[
                {"object_type": logging_enums.ObservableObjectType.ORDER.value}
            ],
        )
        logging_models.EventSubscriptionQueue.objects.create(
            event_subscription=old_subscription,
            offering_uuid=self.offering.uuid,
            object_type=logging_enums.ObservableObjectType.ORDER.value,
        )

        consumer = self._setup_consumer(user=consumer_user)

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        old_path = [m for m in messages if m["topic"].startswith("subscription/")]
        consumer_path = [
            m for m in messages if m["topic"] == f"consumer_{consumer.uuid.hex}"
        ]
        self.assertEqual(len(old_path), 1)
        self.assertEqual(len(consumer_path), 1)

    def test_prepare_messages_consumer_without_queue_skipped(self):
        """A consumer with queue_created=False should produce 0 messages."""
        user = self.fixture.staff
        logging_factories.EventConsumerFactory.for_offering(
            self.offering,
            user=user,
            rmq_username="aabb000000000000000000000000ccdd",
            queue_created=False,
        )

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        consumer_messages = [m for m in messages if m["topic"].startswith("consumer_")]
        self.assertEqual(len(consumer_messages), 0)

    def test_prepare_messages_consumer_no_offering_access_skipped(self):
        """A consumer whose owner lacks access to the offering
        should produce 0 messages for the consumer path."""
        from waldur_core.structure.tests import factories as structure_factories

        user_without_access = structure_factories.UserFactory()
        self._setup_consumer(user=user_without_access)

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        consumer_messages = [m for m in messages if m["topic"].startswith("consumer_")]
        self.assertEqual(len(consumer_messages), 0)

    def test_prepare_messages_respects_object_types_filter(self):
        """Consumer with object_types=['resource'] should not receive 'order'
        events."""
        user = self.fixture.staff
        self._setup_consumer(user=user, object_types=["resource"])

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        consumer_messages = [m for m in messages if m["topic"].startswith("consumer_")]
        self.assertEqual(len(consumer_messages), 0)

    def test_prepare_messages_empty_object_types_sends_all(self):
        """Consumer with empty object_types should receive all event types."""
        user = self.fixture.staff
        consumer = self._setup_consumer(user=user, object_types=[])

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        consumer_messages = [
            m for m in messages if m["topic"] == f"consumer_{consumer.uuid.hex}"
        ]
        self.assertEqual(len(consumer_messages), 1)

    def test_event_consumer_round_trip_for_order_event(self):
        """Round-trip: a registered offering-scoped EventConsumer produces
        exactly one consumer_{uuid} message for an ORDER event. Guards the
        GenericFK content_type/object_id match against the offering."""
        staff = self.fixture.staff
        consumer = logging_factories.EventConsumerFactory.for_offering(
            self.offering,
            user=staff,
            queue_created=True,
            rmq_username="aabb000000000000000000000000ccdd",
        )

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        consumer_messages = [
            m for m in messages if m["topic"] == f"consumer_{consumer.uuid.hex}"
        ]
        self.assertEqual(len(consumer_messages), 1)
        self.assertEqual(consumer_messages[0]["vhost"], staff.uuid.hex)

    def test_standalone_consumer_without_agent_identity_routes(self):
        """An EventConsumer with no linked AgentIdentity still routes: the
        consumer path depends only on the EventConsumer, not the site-agent
        model."""
        from waldur_mastermind.marketplace_site_agent import (
            models as site_agent_models,
        )

        staff = self.fixture.staff
        consumer = logging_factories.EventConsumerFactory.for_offering(
            self.offering,
            user=staff,
            queue_created=True,
            rmq_username="aabb0000000000000000000000001234",
        )
        self.assertFalse(
            site_agent_models.AgentIdentity.objects.filter(
                event_consumer=consumer
            ).exists()
        )

        payload = {"order_uuid": "abc123", "order_state": "pending provider"}
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        consumer_messages = [
            m for m in messages if m["topic"] == f"consumer_{consumer.uuid.hex}"
        ]
        self.assertEqual(len(consumer_messages), 1)


class AgentQueuePayloadEnrichmentTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

    def test_order_enrichment_full_context(self):
        resource = self.fixture.resource
        order = self.fixture.order

        payload = {
            "order_uuid": order.uuid.hex,
            "order_state": order.get_state_display(),
        }
        enriched = marketplace_utils._enrich_order_payload(payload)

        self.assertIn("order_type", enriched)
        self.assertEqual(enriched["resource_uuid"], resource.uuid.hex)
        self.assertEqual(enriched["resource_backend_id"], resource.backend_id)
        self.assertEqual(enriched["resource_name"], resource.name)
        self.assertEqual(enriched["project_uuid"], order.project.uuid.hex)
        self.assertEqual(enriched["project_name"], order.project.name)
        self.assertIn("attributes", enriched)
        self.assertIn("limits", enriched)
        if resource.plan:
            self.assertEqual(enriched["plan_uuid"], resource.plan.uuid.hex)

    def test_user_role_enrichment_covers_deactivated_users(self):
        """Offboarding usually deactivates the user and revokes their roles in
        one flow. User.objects is UserActiveManager and hides inactive users, so
        looking the user up through it stripped email/full_name from exactly the
        role_revoked event a backend needs in order to deprovision the account.
        """
        user = structure_factories.UserFactory(is_active=False)

        enriched = marketplace_utils._enrich_user_role_payload(
            {"user_uuid": user.uuid.hex}
        )

        self.assertEqual(enriched["user_email"], user.email)
        self.assertEqual(enriched["user_full_name"], user.full_name)

    def test_order_enrichment_no_plan(self):
        resource = self.fixture.resource
        resource.plan = None
        resource.save(update_fields=["plan"])

        order = self.fixture.order
        # Ensure the resource queried by enrich function has no plan
        order.resource.plan = None
        order.resource.save(update_fields=["plan"])

        payload = {
            "order_uuid": order.uuid.hex,
            "order_state": order.get_state_display(),
        }
        enriched = marketplace_utils._enrich_order_payload(payload)

        self.assertNotIn("plan_uuid", enriched)

    def test_resource_enrichment_project_context(self):
        resource = self.fixture.resource

        payload = {
            "resource_uuid": resource.uuid.hex,
        }
        enriched = marketplace_utils._enrich_resource_payload(payload)

        self.assertEqual(enriched["resource_name"], resource.name)
        self.assertEqual(enriched["resource_state"], resource.get_state_display())
        self.assertEqual(enriched["project_uuid"], resource.project.uuid.hex)
        self.assertEqual(enriched["project_name"], resource.project.name)
        self.assertIn("limits", enriched)

    def test_user_role_enrichment_user_profile(self):
        user = self.fixture.staff

        payload = {
            "user_uuid": user.uuid.hex,
        }
        enriched = marketplace_utils._enrich_user_role_payload(payload)

        self.assertEqual(enriched["user_email"], user.email)
        self.assertEqual(enriched["user_full_name"], user.full_name)

    def test_enrichment_graceful_on_deleted_order(self):
        fake_uuid = "00000000000000000000000000000001"
        payload = {
            "order_uuid": fake_uuid,
            "order_state": "pending provider",
        }
        enriched = marketplace_utils._enrich_order_payload(payload)

        # Should return payload unchanged when order doesn't exist
        self.assertEqual(enriched["order_uuid"], fake_uuid)
        self.assertNotIn("order_type", enriched)

    def test_enrichment_graceful_on_deleted_resource(self):
        fake_uuid = "00000000000000000000000000000002"
        payload = {
            "resource_uuid": fake_uuid,
        }
        enriched = marketplace_utils._enrich_resource_payload(payload)

        self.assertEqual(enriched["resource_uuid"], fake_uuid)
        self.assertNotIn("resource_name", enriched)

    def test_enrichment_graceful_on_deleted_user(self):
        fake_uuid = "00000000000000000000000000000003"
        payload = {
            "user_uuid": fake_uuid,
        }
        enriched = marketplace_utils._enrich_user_role_payload(payload)

        self.assertEqual(enriched["user_uuid"], fake_uuid)
        self.assertNotIn("user_email", enriched)

    def test_old_path_payloads_not_enriched(self):
        """Old multi-queue path payloads should not contain enrichment fields."""
        user = self.fixture.staff

        old_subscription = logging_models.EventSubscription.objects.create(
            user=user,
            observable_objects=[
                {"object_type": logging_enums.ObservableObjectType.ORDER.value}
            ],
        )
        logging_models.EventSubscriptionQueue.objects.create(
            event_subscription=old_subscription,
            offering_uuid=self.offering.uuid,
            object_type=logging_enums.ObservableObjectType.ORDER.value,
        )

        order = self.fixture.order
        payload = {
            "order_uuid": order.uuid.hex,
            "order_state": order.get_state_display(),
        }
        messages = marketplace_utils.prepare_messages(
            self.offering, payload, logging_enums.ObservableObjectType.ORDER
        )

        old_path_messages = [
            m for m in messages if m["topic"].startswith("subscription/")
        ]
        self.assertGreaterEqual(len(old_path_messages), 1)

        for msg in old_path_messages:
            parsed = json.loads(msg["payload"])
            self.assertNotIn("order_type", parsed)
            self.assertNotIn("object_type", parsed)


@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.delete_queue")
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.delete_rabbitmq_user"
)
class AgentQueueCleanupTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

    def test_agent_identity_deletion_cleans_up_queue(
        self, mock_delete_user, mock_delete_queue
    ):
        user = self.fixture.staff
        agent_identity = factories.make_agent_with_consumer(
            offering=self.offering,
            user=user,
            name="Cleanup Agent",
            rmq_username="aabb000000000000000000000000ccdd",
            queue_created=True,
        )
        expected_queue_name = f"consumer_{agent_identity.event_consumer.uuid.hex}"
        expected_vhost = user.uuid.hex

        agent_identity.delete()

        mock_delete_queue.assert_called_once_with(expected_vhost, expected_queue_name)

    def test_agent_identity_deletion_deletes_rmq_user(
        self, mock_delete_user, mock_delete_queue
    ):
        user = self.fixture.staff
        rmq_username = "aabb000000000000000000000000ccdd"
        factories.make_agent_with_consumer(
            offering=self.offering,
            user=user,
            name="Cleanup Agent Sub",
            rmq_username=rmq_username,
            queue_created=True,
        ).delete()

        mock_delete_user.assert_called_once_with(rmq_username)

    def test_agent_identity_deletion_no_subscription_left(
        self, mock_delete_user, mock_delete_queue
    ):
        """Ensure no EventSubscription is created or left behind."""
        user = self.fixture.staff
        initial_count = logging_models.EventSubscription.objects.count()
        factories.make_agent_with_consumer(
            offering=self.offering,
            user=user,
            name="Cleanup Agent NoSub",
            rmq_username="aabb000000000000000000000000ccdd",
            queue_created=True,
        ).delete()

        self.assertEqual(
            logging_models.EventSubscription.objects.count(), initial_count
        )


@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.delete_queue")
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.list_all_subscription_queues"
)
class AgentQueueOrphanCleanupTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

    def test_orphan_cleanup_handles_consumer_queues(
        self, mock_list_queues, mock_delete_queue
    ):
        fake_uuid = "aabbccdd00000000000000000000dead"
        mock_list_queues.return_value = [
            {
                "vhost": "somevhost",
                "queues": [
                    {
                        "name": f"consumer_{fake_uuid}",
                        "messages": 0,
                        "consumers": 0,
                    }
                ],
            }
        ]

        logging_tasks.cleanup_orphan_subscription_queues()

        mock_delete_queue.assert_called_once_with("somevhost", f"consumer_{fake_uuid}")

    def test_orphan_cleanup_preserves_valid_consumer_queues(
        self, mock_list_queues, mock_delete_queue
    ):
        user = self.fixture.staff
        consumer = logging_factories.EventConsumerFactory.for_offering(
            self.offering,
            user=user,
            rmq_username="aabb000000000000000000000000ccdd",
            queue_created=True,
        )

        mock_list_queues.return_value = [
            {
                "vhost": user.uuid.hex,
                "queues": [
                    {
                        "name": f"consumer_{consumer.uuid.hex}",
                        "messages": 5,
                        "consumers": 1,
                    }
                ],
            }
        ]

        logging_tasks.cleanup_orphan_subscription_queues()

        mock_delete_queue.assert_not_called()


@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
)
class AgentQueueBackwardCompatTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

    def test_old_register_event_subscription_still_works(
        self,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Backward Compat Agent"
        )
        url = _agent_identity_url(agent_identity, action="register_event_subscription")

        payload = {
            "observable_object_type": logging_enums.ObservableObjectType.ORDER.value,
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())

        self.assertEqual(logging_models.EventSubscription.objects.count(), 1)
        mock_create_vhost.assert_called_once()
        mock_create_user.assert_called_once()
        mock_assign_permissions.assert_called_once()


class AgentQueueParseTest(test.APITestCase):
    def test_parse_consumer_queue_name_valid(self):
        uuid_hex = "aabb000000000000000000000000ccdd"
        result = logging_utils.parse_consumer_queue_name(f"consumer_{uuid_hex}")
        self.assertEqual(result, uuid_hex)

    def test_parse_consumer_queue_name_invalid(self):
        result = logging_utils.parse_consumer_queue_name(
            "subscription_abc_offering_def_order"
        )
        self.assertIsNone(result)

    def test_parse_consumer_queue_name_wrong_length(self):
        # Not a 32-char UUID hex — must not match.
        self.assertIsNone(logging_utils.parse_consumer_queue_name("consumer_abc123"))

    def test_parse_consumer_queue_name_empty(self):
        result = logging_utils.parse_consumer_queue_name("consumer_")
        self.assertIsNone(result)


@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
)
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.create_queue")
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.get_user")
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.list_rabbitmq_vhost_permissions"
)
class AgentQueueFastPathRefreshTest(test.APITestCase):
    """Cover the 200-OK fast path: password refresh, object_types update, fallback."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

    def _make_url(self, agent_identity):
        return _agent_identity_url(agent_identity, action="register_queue")

    def _prime_fast_path(self, mocks):
        """First register call sets up RMQ state, then arrange mocks for the
        existing-and-valid branch on a follow-up call."""
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Fast Path Agent"
        )
        url = self._make_url(agent_identity)

        response1 = self.client.post(url, {})
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)
        agent_identity.refresh_from_db()
        consumer = agent_identity.event_consumer

        mocks["get_user"].return_value = {"name": consumer.rmq_username}
        mocks["list_permissions"].return_value = [consumer.rmq_username]
        return user, agent_identity, url

    def test_fast_path_refreshes_password(
        self,
        mock_list_permissions,
        mock_get_user,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user, agent_identity, url = self._prime_fast_path(
            {"get_user": mock_get_user, "list_permissions": mock_list_permissions}
        )
        consumer = agent_identity.event_consumer

        response2 = self.client.post(url, {})
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        # create_rabbitmq_user called twice: initial 201 + fast-path refresh.
        self.assertEqual(mock_create_user.call_count, 2)
        refresh_call = mock_create_user.call_args_list[1]
        self.assertEqual(refresh_call.args[0], consumer.rmq_username)
        self.assertEqual(refresh_call.args[1], user.auth_token.key)

    def test_fast_path_updates_object_types(
        self,
        mock_list_permissions,
        mock_get_user,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        _, agent_identity, url = self._prime_fast_path(
            {"get_user": mock_get_user, "list_permissions": mock_list_permissions}
        )

        response2 = self.client.post(
            url, {"object_types": ["order", "resource"]}, format="json"
        )
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        agent_identity.refresh_from_db()
        consumer = agent_identity.event_consumer
        consumer.refresh_from_db()
        self.assertEqual(consumer.object_types, ["order", "resource"])
        self.assertEqual(
            set(response2.json()["observable_object_types"]),
            {"order", "resource"},
        )

    def test_fast_path_no_change_when_object_types_match(
        self,
        mock_list_permissions,
        mock_get_user,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        user = self.fixture.staff
        self.client.force_login(user)

        agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Stable Types Agent"
        )
        url = self._make_url(agent_identity)

        response1 = self.client.post(url, {"object_types": ["order"]}, format="json")
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        agent_identity.refresh_from_db()
        consumer = agent_identity.event_consumer
        baseline_modified = consumer.modified

        mock_get_user.return_value = {"name": consumer.rmq_username}
        mock_list_permissions.return_value = [consumer.rmq_username]

        response2 = self.client.post(url, {"object_types": ["order"]}, format="json")
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        consumer.refresh_from_db()
        # object_types match → no redundant save on the consumer, so its
        # modified timestamp is unchanged.
        self.assertEqual(consumer.modified, baseline_modified)

    def test_fast_path_falls_back_when_password_refresh_fails(
        self,
        mock_list_permissions,
        mock_get_user,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        # First call must succeed (initial registration).
        mock_create_user.return_value = True
        _, agent_identity, url = self._prime_fast_path(
            {"get_user": mock_get_user, "list_permissions": mock_list_permissions}
        )
        initial_rmq_username = agent_identity.event_consumer.rmq_username

        # Second call: password refresh fails, but the recreate path succeeds.
        mock_create_user.side_effect = [False, True]

        with mock.patch(
            "waldur_core.logging.backend.RabbitMQManagementBackend.delete_rabbitmq_user"
        ):
            response2 = self.client.post(url, {})

        # Recreate path issues a 201 with a fresh rmq_username.
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(response2.json()["rmq_username"], initial_rmq_username)


@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
)
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.create_queue")
class AgentQueueOwnershipTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

    def _make_url(self, agent_identity):
        return _agent_identity_url(agent_identity, action="register_queue")

    def test_cross_user_re_registration_returns_409(
        self,
        mock_create_queue,
        mock_assign_permissions,
        mock_create_user,
        mock_create_vhost,
    ):
        from waldur_core.structure.tests import factories as structure_factories

        user_a = self.fixture.staff
        user_b = structure_factories.UserFactory(is_staff=True)

        agent_identity = factories.AgentIdentityFactory(
            offering=self.offering, name="Owned Agent"
        )
        url = self._make_url(agent_identity)

        self.client.force_login(user_a)
        self.assertEqual(self.client.post(url, {}).status_code, status.HTTP_201_CREATED)

        # Reset mock counters so we can assert no RMQ side effects on takeover.
        mock_create_vhost.reset_mock()
        mock_create_user.reset_mock()
        mock_assign_permissions.reset_mock()
        mock_create_queue.reset_mock()

        self.client.force_login(user_b)
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        mock_create_vhost.assert_not_called()
        mock_create_user.assert_not_called()
        mock_assign_permissions.assert_not_called()
        mock_create_queue.assert_not_called()


@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.delete_rabbitmq_user"
)
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.delete_queue")
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.get_user")
class AgentQueueStaleAndDanglingCleanupTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

    def _make_identity(self, user=None):
        from waldur_mastermind.marketplace_site_agent import models as site_models

        if user is None:
            user = self.fixture.staff
        identity = factories.make_agent_with_consumer(
            offering=self.offering,
            user=user,
            name="Cleanup Subject",
            rmq_username="aabb000000000000000000000000ccdd",
            queue_created=True,
        )
        return identity, site_models

    @mock.patch("waldur_mastermind.marketplace_site_agent.tasks.get_active_tokens")
    def test_stale_cleanup_clears_state_when_owner_has_no_active_token(
        self,
        mock_get_active_tokens,
        mock_get_user,
        mock_delete_queue,
        mock_delete_rmq_user,
    ):
        from waldur_mastermind.marketplace_site_agent import (
            tasks as site_agent_tasks,
        )

        identity, _ = self._make_identity()
        # No active tokens → the consumer's owner is stale.
        mock_get_active_tokens.return_value.values_list.return_value = []

        site_agent_tasks.cleanup_stale_agent_queues()

        consumer = identity.event_consumer
        consumer.refresh_from_db()
        self.assertFalse(consumer.queue_created)
        self.assertEqual(consumer.rmq_username, "")
        mock_delete_queue.assert_called_once()
        mock_delete_rmq_user.assert_called_once_with("aabb000000000000000000000000ccdd")

    @mock.patch("waldur_mastermind.marketplace_site_agent.tasks.get_active_tokens")
    def test_stale_cleanup_skips_active_owners(
        self,
        mock_get_active_tokens,
        mock_get_user,
        mock_delete_queue,
        mock_delete_rmq_user,
    ):
        from waldur_mastermind.marketplace_site_agent import (
            tasks as site_agent_tasks,
        )

        identity, _ = self._make_identity()
        # Owner still has an active token → not stale.
        mock_get_active_tokens.return_value.values_list.return_value = [
            identity.event_consumer.user_id
        ]

        site_agent_tasks.cleanup_stale_agent_queues()

        consumer = identity.event_consumer
        consumer.refresh_from_db()
        self.assertTrue(consumer.queue_created)
        mock_delete_queue.assert_not_called()
        mock_delete_rmq_user.assert_not_called()

    @mock.patch("waldur_mastermind.marketplace_site_agent.tasks.get_active_tokens")
    def test_stale_cleanup_skips_owner_with_live_pat(
        self,
        mock_get_active_tokens,
        mock_get_user,
        mock_delete_queue,
        mock_delete_rmq_user,
    ):
        """A PAT-backed owner with no active DRF token must NOT be reaped:
        the agent's credential is a live PAT, not a session token."""
        from datetime import timedelta as _td

        from django.utils import timezone as _tz

        from waldur_core.core.models import PersonalAccessToken
        from waldur_mastermind.marketplace_site_agent import tasks as site_agent_tasks

        identity, _ = self._make_identity()
        mock_get_active_tokens.return_value.values_list.return_value = []
        _, prefix, token_hash = PersonalAccessToken.generate_token(
            _tz.now() + _td(days=30)
        )
        PersonalAccessToken.objects.create(
            user=identity.event_consumer.user,
            name="agent-pat",
            token_prefix=prefix,
            token_hash=token_hash,
            expires_at=_tz.now() + _td(days=30),
            is_active=True,
        )

        site_agent_tasks.cleanup_stale_agent_queues()

        consumer = identity.event_consumer
        consumer.refresh_from_db()
        self.assertTrue(consumer.queue_created)
        mock_delete_queue.assert_not_called()
        mock_delete_rmq_user.assert_not_called()

    @mock.patch("waldur_mastermind.marketplace_site_agent.tasks.get_active_tokens")
    def test_stale_cleanup_reaps_owner_with_expired_pat(
        self,
        mock_get_active_tokens,
        mock_get_user,
        mock_delete_queue,
        mock_delete_rmq_user,
    ):
        """An expired PAT and no DRF token → owner is stale, consumer reaped."""
        from datetime import timedelta as _td

        from django.utils import timezone as _tz

        from waldur_core.core.models import PersonalAccessToken
        from waldur_mastermind.marketplace_site_agent import tasks as site_agent_tasks

        identity, _ = self._make_identity()
        mock_get_active_tokens.return_value.values_list.return_value = []
        _, prefix, token_hash = PersonalAccessToken.generate_token(
            _tz.now() - _td(days=1)
        )
        PersonalAccessToken.objects.create(
            user=identity.event_consumer.user,
            name="agent-pat-expired",
            token_prefix=prefix,
            token_hash=token_hash,
            expires_at=_tz.now() - _td(days=1),
            is_active=True,
        )

        site_agent_tasks.cleanup_stale_agent_queues()

        consumer = identity.event_consumer
        consumer.refresh_from_db()
        self.assertFalse(consumer.queue_created)
        self.assertEqual(consumer.rmq_username, "")
        mock_delete_queue.assert_called_once()
        mock_delete_rmq_user.assert_called_once()

    def test_dangling_cleanup_clears_state_when_rmq_user_missing(
        self,
        mock_get_user,
        mock_delete_queue,
        mock_delete_rmq_user,
    ):
        from waldur_mastermind.marketplace_site_agent import (
            tasks as site_agent_tasks,
        )

        identity, _ = self._make_identity()
        mock_get_user.return_value = None

        site_agent_tasks.cleanup_dangling_agent_queues()

        consumer = identity.event_consumer
        consumer.refresh_from_db()
        self.assertFalse(consumer.queue_created)
        self.assertEqual(consumer.rmq_username, "")
        mock_delete_queue.assert_not_called()

    def test_dangling_cleanup_leaves_state_when_rmq_user_present(
        self,
        mock_get_user,
        mock_delete_queue,
        mock_delete_rmq_user,
    ):
        from waldur_mastermind.marketplace_site_agent import (
            tasks as site_agent_tasks,
        )

        identity, _ = self._make_identity()
        mock_get_user.return_value = {"name": identity.event_consumer.rmq_username}

        site_agent_tasks.cleanup_dangling_agent_queues()

        consumer = identity.event_consumer
        consumer.refresh_from_db()
        self.assertTrue(consumer.queue_created)


class AgentRmqPasswordResolutionTest(test.APITestCase):
    """The RMQ password comes from the presented PAT, else the DRF token."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()

    def test_password_from_pat_when_pat_authenticated(self):
        from datetime import timedelta as _td

        from django.utils import timezone as _tz

        from waldur_core.core.models import PersonalAccessToken
        from waldur_mastermind.marketplace_site_agent.views import (
            _resolve_agent_rmq_password,
        )

        user = self.fixture.staff
        full_token, prefix, token_hash = PersonalAccessToken.generate_token(
            _tz.now() + _td(days=30)
        )
        pat = PersonalAccessToken.objects.create(
            user=user,
            name="agent",
            token_prefix=prefix,
            token_hash=token_hash,
            expires_at=_tz.now() + _td(days=30),
            is_active=True,
        )
        request = test.APIRequestFactory().post(
            "/", HTTP_AUTHORIZATION=f"Bearer {full_token}"
        )
        request.auth = pat
        request.user = user

        self.assertEqual(_resolve_agent_rmq_password(request), full_token)

    def test_password_falls_back_to_drf_token_without_pat(self):
        from rest_framework.authtoken.models import Token

        from waldur_mastermind.marketplace_site_agent.views import (
            _resolve_agent_rmq_password,
        )

        user = self.fixture.staff
        request = test.APIRequestFactory().post("/")
        request.auth = None
        request.user = user

        self.assertEqual(
            _resolve_agent_rmq_password(request),
            Token.objects.get(user=user).key,
        )


@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.list_all_subscription_queues",
    return_value=[],
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.list_rabbitmq_users",
    return_value=[],
)
class AgentConnectionStatsTest(test.APITestCase):
    """F5b: each agent's event_subscriptions is scoped to its own owner, not the
    global subscription set."""

    URL = "/api/marketplace-site-agent-connection-stats/"

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()

    def test_subscriptions_scoped_to_each_agent_owner(self, mock_users, mock_queues):
        from waldur_core.structure.tests import factories as structure_factories

        support = structure_factories.UserFactory()
        support.is_support = True
        support.save()

        user_a = structure_factories.UserFactory()
        user_b = structure_factories.UserFactory()
        # Each agent is linked to a consumer owned by the respective user; the
        # connection-stats endpoint scopes subscriptions by the consumer owner.
        factories.make_agent_with_consumer(
            offering=self.offering, user=user_a, name="A"
        )
        factories.make_agent_with_consumer(
            offering=self.offering, user=user_b, name="B"
        )
        logging_models.EventSubscription.objects.create(
            user=user_a, observable_objects=[{"object_type": "order"}]
        )
        logging_models.EventSubscription.objects.create(
            user=user_b, observable_objects=[{"object_type": "resource"}]
        )

        self.client.force_authenticate(user=support)
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

        agents = {a["name"]: a for a in response.json()["agents"]}
        # Before the fix each agent showed BOTH subscriptions; now exactly one,
        # and A's and B's are different.
        a_subs = agents["A"]["event_subscriptions"]
        b_subs = agents["B"]["event_subscriptions"]
        self.assertEqual(len(a_subs), 1)
        self.assertEqual(len(b_subs), 1)
        self.assertNotEqual(a_subs[0]["uuid"], b_subs[0]["uuid"])
        self.assertEqual(response.json()["summary"]["connected_agents"], 0)


@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
)
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.create_queue")
# The re-registration tests below take the "already registered" branch, which
# also calls get_user / list_rabbitmq_vhost_permissions and, when it judges the
# RMQ state stale, delete_rabbitmq_user + delete_queue. Without these mocks the
# suite reaches for a real broker on localhost:15672 — which passes on a machine
# running the dev stack and fails in CI.
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.get_user")
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.list_rabbitmq_vhost_permissions"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.delete_rabbitmq_user"
)
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.delete_queue")
class AgentConsumerLifecycleTest(test.APITestCase):
    """Regressions on the boundary between a site agent's consumer and the
    generic /api/event-consumers/ endpoints, plus consumer teardown."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = enums.SITE_AGENT_OFFERING
        self.offering.save()
        self.staff = self.fixture.staff

    def _register(self, data=None):
        agent_identity = factories.AgentIdentityFactory(offering=self.offering)
        url = _agent_identity_url(agent_identity, action="register_queue")
        self.client.force_login(self.staff)
        response = self.client.post(url, data or {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.json())
        agent_identity.refresh_from_db()
        return agent_identity

    def test_agent_consumer_is_not_reused_by_generic_register(self, *mocks):
        """POSTing the same offering binding to /api/event-consumers/register/
        must NOT hijack the running agent's consumer and retune its filters."""
        agent_identity = self._register({"object_types": ["order"]})
        agent_consumer = agent_identity.event_consumer

        self.client.force_login(self.staff)
        response = self.client.post(
            "http://testserver" + reverse("event-consumer-register"),
            {
                "object_types": ["resource"],
                "scopes": [
                    {"type": "offering", "uuid": self.offering.uuid.hex},
                ],
            },
            format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_200_OK, status.HTTP_201_CREATED),
            response.json(),
        )
        agent_consumer.refresh_from_db()
        self.assertEqual(agent_consumer.object_types, ["order"])

    def test_agent_consumer_is_not_listed_or_deletable(self, *mocks):
        """DELETE on the agent's consumer would tear down its live queue, since
        AgentIdentity.event_consumer is only SET_NULL."""
        agent_identity = self._register()
        consumer = agent_identity.event_consumer

        self.client.force_login(self.staff)
        list_response = self.client.get(
            "http://testserver" + reverse("event-consumer-list")
        )
        results = list_response.data
        if isinstance(results, dict):
            results = results["results"]
        listed = [c["uuid"] for c in results]
        self.assertNotIn(consumer.uuid.hex, listed)

        detail = "http://testserver" + reverse(
            "event-consumer-detail", kwargs={"uuid": consumer.uuid.hex}
        )
        self.assertEqual(
            self.client.delete(detail).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertTrue(
            logging_models.EventConsumer.objects.filter(id=consumer.id).exists()
        )

    def test_omitted_object_types_does_not_widen_the_filter(self, *mocks):
        """A restarted agent that stops sending object_types must keep its
        narrow filter, not silently reopen the full firehose."""
        agent_identity = self._register({"object_types": ["order"]})
        url = _agent_identity_url(agent_identity, action="register_queue")

        self.client.force_login(self.staff)
        response = self.client.post(url, {})
        # Either exit path is fine (the fast path needs live RMQ state); what
        # matters is that the filter is not widened.
        self.assertIn(
            response.status_code,
            (status.HTTP_200_OK, status.HTTP_201_CREATED),
            response.json(),
        )

        agent_identity.event_consumer.refresh_from_db()
        self.assertEqual(agent_identity.event_consumer.object_types, ["order"])

    def test_explicit_empty_object_types_widens_to_all(self, *mocks):
        """An EXPLICIT [] still means 'all types' — only omission is a no-op."""
        agent_identity = self._register({"object_types": ["order"]})
        url = _agent_identity_url(agent_identity, action="register_queue")

        self.client.force_login(self.staff)
        self.client.post(url, {"object_types": []}, format="json")

        agent_identity.event_consumer.refresh_from_db()
        self.assertEqual(agent_identity.event_consumer.object_types, [])

    def test_binding_is_reconciled_when_the_offering_changes(self, *mocks):
        """`offering` is writable on the agent, so re-registration must repoint
        the binding — otherwise the agent listens to its old offering forever."""
        agent_identity = self._register()
        consumer = agent_identity.event_consumer

        new_offering = marketplace_fixtures.MarketplaceFixture().offering
        new_offering.type = enums.SITE_AGENT_OFFERING
        new_offering.customer = self.offering.customer
        new_offering.save()
        agent_identity.offering = new_offering
        agent_identity.save(update_fields=["offering"])

        url = _agent_identity_url(agent_identity, action="register_queue")
        self.client.force_login(self.staff)
        self.client.post(url, {})

        bindings = [s.object_id for s in consumer.scopes.all()]
        self.assertEqual(bindings, [new_offering.id])

    def test_deleting_agent_identity_deletes_the_consumer(self, *mocks):
        """A surviving consumer keeps queue_created=True and its binding, so the
        dispatcher would go on publishing into a destroyed queue."""
        agent_identity = self._register()
        consumer_id = agent_identity.event_consumer_id

        agent_identity.delete()  # RMQ teardown is mocked at class level

        self.assertFalse(
            logging_models.EventConsumer.objects.filter(id=consumer_id).exists()
        )
