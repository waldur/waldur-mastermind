"""Ephemeral (browser) event consumers and the idle sweep that removes them."""

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import PersonalAccessToken
from waldur_core.logging import models as logging_models
from waldur_core.logging import serializers, tasks
from waldur_core.logging.exceptions import RabbitMQError
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories

URL = "/api/event-consumers/register/"
BACKEND = "waldur_core.logging.backend.RabbitMQManagementBackend"


@mock.patch(f"{BACKEND}.list_rabbitmq_vhost_permissions")
@mock.patch(f"{BACKEND}.get_user")
@mock.patch(f"{BACKEND}.create_queue")
@mock.patch(f"{BACKEND}.assign_rabbitmq_vhost_permissions")
@mock.patch(f"{BACKEND}.create_rabbitmq_user")
@mock.patch(f"{BACKEND}.create_rabbitmq_virtual_host")
class EphemeralRegistrationTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        # A browser tab authenticates with the user's DRF token.
        self.client.force_authenticate(self.user, token=self.user.auth_token)
        self.body = {"scopes": [{"type": "user", "uuid": self.user.uuid.hex}]}

    def register(self, mock_get_user, mock_vhost_perms, **extra):
        response = self.client.post(URL, {**self.body, **extra}, format="json")
        self.assertIn(
            response.status_code,
            (status.HTTP_200_OK, status.HTTP_201_CREATED),
            response.content,
        )
        consumer = logging_models.EventConsumer.objects.get(user=self.user)
        # Make the next registration take the fast path.
        mock_get_user.return_value = {"name": consumer.rmq_username}
        mock_vhost_perms.return_value = [consumer.rmq_username]
        return consumer

    def test_consumer_is_not_ephemeral_by_default(
        self, _vhost, _user, _perms, _queue, mock_get_user, mock_vhost_perms
    ):
        consumer = self.register(mock_get_user, mock_vhost_perms)
        self.assertFalse(consumer.ephemeral)

    def test_client_can_register_an_ephemeral_consumer(
        self, _vhost, _user, _perms, _queue, mock_get_user, mock_vhost_perms
    ):
        consumer = self.register(mock_get_user, mock_vhost_perms, ephemeral=True)
        self.assertTrue(consumer.ephemeral)

    def test_registration_without_the_flag_makes_the_consumer_permanent(
        self, _vhost, _user, _perms, _queue, mock_get_user, mock_vhost_perms
    ):
        self.register(mock_get_user, mock_vhost_perms, ephemeral=True)
        consumer = self.register(mock_get_user, mock_vhost_perms)
        self.assertFalse(consumer.ephemeral)
        self.assertEqual(
            logging_models.EventConsumer.objects.filter(user=self.user).count(), 1
        )

    def test_existing_browser_consumer_becomes_ephemeral(
        self, _vhost, _user, _perms, _queue, mock_get_user, mock_vhost_perms
    ):
        # Registered by a UI that predates the flag: permanent, DRF token,
        # bound to the user's own scope. The new UI's flag must take effect.
        self.register(mock_get_user, mock_vhost_perms)
        consumer = self.register(mock_get_user, mock_vhost_perms, ephemeral=True)
        self.assertTrue(consumer.ephemeral)

    def test_integration_consumer_does_not_become_ephemeral(
        self, _vhost, _user, _perms, _queue, mock_get_user, mock_vhost_perms
    ):
        # Last registered with a PAT (an integration): a browser tab sharing
        # the binding set must not put its queue in the sweep.
        consumer = self.register(mock_get_user, mock_vhost_perms)
        consumer.auth_kind = "pat"
        consumer.save(update_fields=["auth_kind"])

        consumer = self.register(mock_get_user, mock_vhost_perms, ephemeral=True)
        self.assertFalse(consumer.ephemeral)

    def test_consumer_without_the_self_binding_does_not_become_ephemeral(
        self, _vhost, _user, _perms, _queue, mock_get_user, mock_vhost_perms
    ):
        customer = structure_factories.CustomerFactory()
        customer.add_user(self.user, CustomerRole.OWNER)
        self.body = {"scopes": [{"type": "customer", "uuid": customer.uuid.hex}]}

        self.register(mock_get_user, mock_vhost_perms)
        consumer = self.register(mock_get_user, mock_vhost_perms, ephemeral=True)
        self.assertFalse(consumer.ephemeral)

    def test_re_registration_restarts_the_idle_clock(
        self, _vhost, _user, _perms, _queue, mock_get_user, mock_vhost_perms
    ):
        consumer = self.register(mock_get_user, mock_vhost_perms, ephemeral=True)
        consumer.unread_since = timezone.now()
        consumer.save(update_fields=["unread_since"])

        consumer = self.register(mock_get_user, mock_vhost_perms, ephemeral=True)
        self.assertIsNone(consumer.unread_since)

    def test_listing_exposes_the_flag_and_idle_clock(
        self, _vhost, _user, _perms, _queue, mock_get_user, mock_vhost_perms
    ):
        self.register(mock_get_user, mock_vhost_perms, ephemeral=True)
        response = self.client.get("/api/event-consumers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.json()[0]
        self.assertTrue(row["ephemeral"])
        self.assertIsNone(row["unread_since"])

    @mock.patch(f"{BACKEND}.delete_rabbitmq_user", return_value=True)
    @mock.patch(f"{BACKEND}.delete_queue", return_value=True)
    @mock.patch(f"{BACKEND}.list_queue_readers", return_value={})
    def test_swept_consumer_is_replaced_on_re_registration(
        self,
        _readers,
        _delete_queue,
        _delete_user,
        _vhost,
        _user,
        _perms,
        _queue,
        mock_get_user,
        mock_vhost_perms,
    ):
        swept = self.register(mock_get_user, mock_vhost_perms, ephemeral=True)
        swept.unread_since = timezone.now() - timedelta(hours=25)
        swept.save(update_fields=["unread_since"])
        tasks.delete_idle_ephemeral_event_consumers()

        response = self.client.post(
            URL, {**self.body, "ephemeral": True}, format="json"
        )

        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.content
        )
        fresh = logging_models.EventConsumer.objects.get(user=self.user)
        self.assertNotEqual(fresh.uuid, swept.uuid)
        self.assertEqual(response.json()["queue_name"], fresh.queue_name)
        self.assertTrue(fresh.ephemeral)


class WebhookSerializerTest(test.APITestCase):
    def test_webhook_input_does_not_offer_ephemeral(self):
        self.assertNotIn(
            "ephemeral", serializers.EventConsumerWebhookSerializer().fields
        )


@override_config(EVENT_CONSUMER_EPHEMERAL_IDLE_HOURS=24)
@mock.patch(f"{BACKEND}.delete_rabbitmq_user", return_value=True)
@mock.patch(f"{BACKEND}.delete_queue", return_value=True)
@mock.patch(f"{BACKEND}.list_queue_readers", return_value={})
class IdleEphemeralConsumerSweepTest(test.APITestCase):
    def setUp(self):
        # UserFactory issues a DRF token, so the owner is active.
        self.user = structure_factories.UserFactory()

    def tokenless_user(self):
        user = structure_factories.UserFactory()
        Token.objects.filter(user=user).delete()
        return user

    def make_consumer(self, user=None, **kwargs):
        defaults = {
            "user": user or self.user,
            "ephemeral": True,
            "rmq_username": "rmquser",
            "queue_created": True,
            "auth_kind": "token",
        }
        defaults.update(kwargs)
        return logging_models.EventConsumer.objects.create(**defaults)

    def readers(self, consumer, count):
        return {(consumer.vhost, consumer.queue_name): count}

    def exists(self, consumer):
        return logging_models.EventConsumer.objects.filter(pk=consumer.pk).exists()

    # -- idle path ---------------------------------------------------------

    def test_unread_queue_starts_the_idle_clock(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        consumer = self.make_consumer()
        mock_readers.return_value = self.readers(consumer, 0)

        with freeze_time("2026-11-03 10:00:00"):
            tasks.delete_idle_ephemeral_event_consumers()
            stamped_at = timezone.now()

        consumer.refresh_from_db()
        self.assertEqual(consumer.unread_since, stamped_at)
        mock_delete_queue.assert_not_called()

    def test_consumer_unread_past_the_threshold_is_deleted_with_its_queue(
        self, mock_readers, mock_delete_queue, mock_delete_user
    ):
        consumer = self.make_consumer(unread_since=timezone.now() - timedelta(hours=25))
        mock_readers.return_value = self.readers(consumer, 0)

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertFalse(self.exists(consumer))
        # Torn down once, by the sweep; the pre_delete handler then has nothing
        # left to do.
        mock_delete_queue.assert_called_once_with(
            self.user.uuid.hex, consumer.queue_name
        )
        mock_delete_user.assert_called_once_with("rmquser")

    def test_consumer_unread_within_the_threshold_is_kept(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        consumer = self.make_consumer(unread_since=timezone.now() - timedelta(hours=23))
        mock_readers.return_value = self.readers(consumer, 0)

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(consumer))
        mock_delete_queue.assert_not_called()

    @override_config(EVENT_CONSUMER_EPHEMERAL_IDLE_HOURS=2)
    def test_threshold_is_configurable(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        consumer = self.make_consumer(unread_since=timezone.now() - timedelta(hours=3))
        mock_readers.return_value = self.readers(consumer, 0)

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertFalse(self.exists(consumer))

    @override_config(EVENT_CONSUMER_EPHEMERAL_IDLE_HOURS=0)
    def test_threshold_is_at_least_an_hour(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        consumer = self.make_consumer(
            unread_since=timezone.now() - timedelta(minutes=30)
        )
        mock_readers.return_value = self.readers(consumer, 0)

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(consumer))

    def test_consumer_with_a_reader_is_kept_and_its_clock_cleared(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        consumer = self.make_consumer(unread_since=timezone.now() - timedelta(days=30))
        mock_readers.return_value = self.readers(consumer, 1)

        tasks.delete_idle_ephemeral_event_consumers()

        consumer.refresh_from_db()
        self.assertIsNone(consumer.unread_since)
        mock_delete_queue.assert_not_called()

    def test_missing_queue_counts_as_unread(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        consumer = self.make_consumer(
            queue_created=False,
            rmq_username="",
            unread_since=timezone.now() - timedelta(hours=25),
        )

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertFalse(self.exists(consumer))

    def test_re_registration_between_read_and_delete_keeps_the_consumer(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        consumer = self.make_consumer(unread_since=timezone.now() - timedelta(hours=25))
        mock_readers.return_value = self.readers(consumer, 0)

        def tab_re_registers(user_id):
            # The registration held the lock first and cleared the clock.
            logging_models.EventConsumer.objects.filter(pk=consumer.pk).update(
                unread_since=None
            )

        with mock.patch.object(
            tasks.utils, "lock_user_registration", side_effect=tab_re_registers
        ):
            tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(consumer))
        mock_delete_queue.assert_not_called()

    # -- owner's credentials ----------------------------------------------

    def test_owner_without_an_active_credential_is_deleted_at_once(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        owner = self.tokenless_user()
        consumer = self.make_consumer(user=owner)
        # Even with a reader still attached: the token it connected with is gone.
        mock_readers.return_value = self.readers(consumer, 1)

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertFalse(self.exists(consumer))
        mock_delete_queue.assert_called_once_with(owner.uuid.hex, consumer.queue_name)

    def test_owner_whose_drf_token_outlived_its_lifetime_is_deleted(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        self.user.token_lifetime = 3600
        self.user.save(update_fields=["token_lifetime"])
        Token.objects.filter(user=self.user).update(
            created=timezone.now() - timedelta(hours=2)
        )
        consumer = self.make_consumer()

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertFalse(self.exists(consumer))

    def test_owner_with_only_a_live_pat_keeps_the_consumer(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        # A regression here would sweep every PAT-backed consumer.
        owner = self.tokenless_user()
        full_token, prefix, token_hash = PersonalAccessToken.generate_token(
            timezone.now() + timedelta(days=30)
        )
        PersonalAccessToken.objects.create(
            user=owner,
            name="ci",
            token_prefix=prefix,
            token_hash=token_hash,
            scopes=[],
            expires_at=timezone.now() + timedelta(days=30),
            is_active=True,
        )
        consumer = self.make_consumer(user=owner, auth_kind="pat")
        mock_readers.return_value = self.readers(consumer, 1)

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(consumer))
        mock_delete_queue.assert_not_called()

    def test_oidc_consumer_is_judged_by_its_readers_not_the_drf_token(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        # An OIDC client's DRF token is created once and never refreshed.
        owner = self.tokenless_user()
        consumer = self.make_consumer(user=owner, auth_kind="oidc")
        mock_readers.return_value = self.readers(consumer, 1)

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(consumer))

    def test_deactivated_owner_with_a_valid_token_is_deleted_at_once(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        consumer = self.make_consumer(auth_kind="oidc")
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertFalse(self.exists(consumer))

    def test_owner_who_logs_in_during_the_sweep_keeps_the_consumer(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        owner = self.tokenless_user()
        consumer = self.make_consumer(user=owner)

        # Inactive when the sweep looks, active again by the time it holds the
        # per-user lock (the user logged in and re-registered in between).
        with mock.patch.object(
            tasks, "_has_active_credential", side_effect=[False, True]
        ):
            tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(consumer))
        mock_delete_queue.assert_not_called()

    # -- what is never swept -------------------------------------------------

    def test_non_ephemeral_consumer_is_never_swept(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        unread = self.make_consumer(
            ephemeral=False, unread_since=timezone.now() - timedelta(days=30)
        )
        tokenless = self.make_consumer(user=self.tokenless_user(), ephemeral=False)

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(unread))
        self.assertTrue(self.exists(tokenless))
        mock_delete_queue.assert_not_called()

    def test_webhook_consumer_is_never_swept(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        consumer = self.make_consumer(
            delivery=logging_models.EventConsumer.Delivery.WEBHOOK,
            queue_created=False,
            rmq_username="",
            unread_since=timezone.now() - timedelta(days=30),
        )

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(consumer))

    # -- RabbitMQ failures -----------------------------------------------------

    def test_unreadable_broker_deletes_nothing_on_any_path(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        idle = self.make_consumer(unread_since=timezone.now() - timedelta(days=30))
        signed_out = self.make_consumer(user=self.tokenless_user())
        mock_readers.side_effect = RabbitMQError("management API down")

        with self.assertLogs("waldur_core.logging.tasks", level="WARNING") as logs:
            tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(idle))
        self.assertTrue(self.exists(signed_out))
        mock_delete_queue.assert_not_called()
        self.assertIn("unable to list", logs.output[0])

    def test_failed_queue_teardown_keeps_the_row(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        consumer = self.make_consumer(user=self.tokenless_user())
        mock_delete_queue.side_effect = RabbitMQError("refused")

        tasks.delete_idle_ephemeral_event_consumers()

        consumer.refresh_from_db()
        self.assertTrue(consumer.queue_created)
        self.assertEqual(consumer.rmq_username, "rmquser")

    def test_failed_user_teardown_keeps_the_row(
        self, mock_readers, mock_delete_queue, mock_delete_user
    ):
        consumer = self.make_consumer(user=self.tokenless_user())
        mock_delete_user.return_value = False

        tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(consumer))

    def test_one_failing_consumer_does_not_stop_the_sweep(
        self, mock_readers, mock_delete_queue, _delete_user
    ):
        first = self.make_consumer(user=self.tokenless_user())
        second = self.make_consumer(user=self.tokenless_user())
        real = tasks._sweep_ephemeral_consumer

        def fail_on_first(rmq_backend, consumer, *args):
            if consumer.pk == first.pk:
                raise ValueError("unexpected")
            return real(rmq_backend, consumer, *args)

        with mock.patch.object(
            tasks, "_sweep_ephemeral_consumer", side_effect=fail_on_first
        ):
            tasks.delete_idle_ephemeral_event_consumers()

        self.assertTrue(self.exists(first))
        self.assertFalse(self.exists(second))
