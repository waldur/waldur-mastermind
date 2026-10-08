import io
import json
import re
from unittest import mock

import sentry_sdk
import yaml
from constance.test import override_config
from django.core.management import call_command
from rest_framework import status, test
from sentry_sdk.transport import Transport

from waldur_core.logging import sentry
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests.factories import UserFactory
from waldur_core.structure.tests.fixtures import add_user_to_project
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.matrix_chat import models, tasks
from waldur_mastermind.matrix_chat.tests import fixtures

WEBHOOK_URL = "/_matrix/app/v1/transactions/"
HS_TOKEN = "test-hs-token-secret"
AS_TOKEN = "test-as-token-secret"
BOT_USER_ID = "@waldur-bot:matrix.example.com"


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class WebhookAuthTest(test.APITestCase):
    def test_valid_hs_token_returns_200(self):
        response = self.client.put(
            f"{WEBHOOK_URL}txn1",
            data={"events": []},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_invalid_hs_token_returns_403(self):
        response = self.client.put(
            f"{WEBHOOK_URL}txn2",
            data={"events": []},
            format="json",
            HTTP_AUTHORIZATION="Bearer wrong-token",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_ascii_hs_token_returns_403(self):
        # compare_digest raises on non-ASCII text, which turned anyone's bad
        # header into a 500 with the real token in the failing frame.
        response = self.client.put(
            f"{WEBHOOK_URL}txn-non-ascii",
            data={"events": []},
            format="json",
            HTTP_AUTHORIZATION="Bearer \xe9" + HS_TOKEN[1:],
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_body_that_is_not_an_object_returns_400(self):
        response = self.client.put(
            f"{WEBHOOK_URL}txn-list",
            data=[{"type": "m.room.message"}],
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.MatrixAppserviceTransaction.objects.filter(
                txn_id="txn-list"
            ).exists()
        )

    def test_events_that_are_not_a_list_return_400(self):
        for i, events in enumerate([None, 1, "m.room.message", {"type": "x"}]):
            with self.subTest(events=events):
                response = self.client.put(
                    f"{WEBHOOK_URL}txn-events-{i}",
                    data={"events": events},
                    format="json",
                    HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertFalse(
                    models.MatrixAppserviceTransaction.objects.filter(
                        txn_id=f"txn-events-{i}"
                    ).exists()
                )

    def test_missing_auth_header_returns_403(self):
        response = self.client.put(
            f"{WEBHOOK_URL}txn3",
            data={"events": []},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_empty_hs_token_config_returns_403(self):
        with override_config(MATRIX_APPSERVICE_HS_TOKEN=""):
            response = self.client.put(
                f"{WEBHOOK_URL}txn4",
                data={"events": []},
                format="json",
                HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}",
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_disabled_integration_returns_200_no_processing(self):
        # When MATRIX_ENABLED flips off the homeserver should not be told to
        # retry, and no transaction record should be created.
        with override_config(MATRIX_ENABLED=False):
            response = self.client.put(
                f"{WEBHOOK_URL}txn-disabled",
                data={"events": [{"type": "m.room.message"}]},
                format="json",
                HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertFalse(
                models.MatrixAppserviceTransaction.objects.filter(
                    txn_id="txn-disabled"
                ).exists()
            )


class _CaptureEvents(Transport):
    def __init__(self):
        super().__init__()
        self.events = []

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event:
            self.events.append(event)


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class WebhookErrorReportTest(test.APITestCase):
    # The first call after the token check, so the error does not depend on
    # which events later steps let through.
    @mock.patch(
        "waldur_mastermind.matrix_chat.models.MatrixAppserviceTransaction.objects.get_or_create",
        side_effect=RuntimeError("database down"),
    )
    def test_an_error_after_the_token_check_does_not_report_the_token(self, _):
        # Sentry records the locals of every frame, and the view held the
        # homeserver token in two of them.
        transport = _CaptureEvents()
        client = sentry_sdk.Client(
            dsn="https://key@sentry.invalid/1",
            transport=transport,
            event_scrubber=sentry.event_scrubber(),
            before_send=sentry.before_send,
        )
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            try:
                self.client.put(
                    f"{WEBHOOK_URL}txn-error",
                    data={"events": [{"type": "m.room.message"}]},
                    format="json",
                    HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}",
                )
            except RuntimeError:
                scope.capture_exception()
        client.flush()

        # The exception, and Django's error log of the request.
        self.assertTrue(transport.events)
        self.assertNotIn(HS_TOKEN, json.dumps(transport.events))


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class IdempotencyTest(test.APITestCase):
    @mock.patch("waldur_mastermind.matrix_chat.tasks.process_appservice_events.delay")
    def test_same_txn_id_processed_once(self, mock_delay):
        room_id = fixtures.MatrixChatFixture().matrix_room.room_id
        events = [
            {"type": "m.room.message", "room_id": room_id, "content": {"body": "hi"}}
        ]
        url = f"{WEBHOOK_URL}txn-dedup"

        response1 = self.client.put(
            url,
            data={"events": events},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}",
        )
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_delay.call_count, 1)

        response2 = self.client.put(
            url,
            data={"events": events},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}",
        )
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        # Should not dispatch again
        self.assertEqual(mock_delay.call_count, 1)

    def test_transaction_record_created(self):
        self.client.put(
            f"{WEBHOOK_URL}txn-record",
            data={"events": [{"type": "test"}]},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}",
        )
        txn = models.MatrixAppserviceTransaction.objects.get(txn_id="txn-record")
        self.assertEqual(txn.event_count, 1)


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
@mock.patch("waldur_mastermind.matrix_chat.tasks.process_appservice_events.delay")
class WebhookRoomFilterTest(test.APITestCase):
    # The appservice namespace covers every local user, so the homeserver sends
    # Waldur every message they see: direct messages, the #admins room with
    # the passwords its commands echo. None of it may reach the task queue.

    def _put(self, txn_id, events):
        return self.client.put(
            f"{WEBHOOK_URL}{txn_id}",
            data={"events": events},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}",
        )

    def _message(self, room_id, body):
        return {
            "type": "m.room.message",
            "room_id": room_id,
            "sender": "@alice:matrix.example.com",
            "content": {"msgtype": "m.text", "body": body},
        }

    def test_only_events_of_rooms_waldur_manages_are_queued(self, mock_delay):
        managed = fixtures.MatrixChatFixture().matrix_room.room_id
        command = self._message(managed, "!status")

        response = self._put(
            "txn-mixed",
            [
                self._message("!dm:matrix.example.com", "a private message"),
                command,
                self._message("!admins:matrix.example.com", "!admin users ..."),
            ],
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_delay.assert_called_once_with("txn-mixed", [command])

    def test_nothing_is_queued_without_a_managed_room(self, mock_delay):
        response = self._put(
            "txn-unmanaged",
            [self._message("!dm:matrix.example.com", "a private message")],
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_delay.assert_not_called()
        # Still recorded, so a retried transaction is not looked at again.
        self.assertTrue(
            models.MatrixAppserviceTransaction.objects.filter(
                txn_id="txn-unmanaged"
            ).exists()
        )

    def test_malformed_events_are_dropped(self, mock_delay):
        # The transaction is recorded before the events are looked at, so an
        # error here would also drop the homeserver's retry.
        response = self._put(
            "txn-garbage",
            [
                "not an event",
                {"room_id": None},
                {"room_id": ["x"]},
                {"room_id": {"a": 1}},
            ],
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_delay.assert_not_called()


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
# The dispatch path now denies bot commands from senders without a Waldur
# role on the room's project and replies via matrix_client.send_reply, which
# POSTs to MATRIX_HOMESERVER_URL. With the fake matrix.example.com URL these
# tests use, that POST hangs on CI runners that drop outbound TCP. Stub the
# access check (return a truthy object so the dispatch path proceeds to the
# bot-command branch) and the reply call (defense in depth) at the class
# level — gating behaviour itself is covered by CommandHandlerTest.
@mock.patch(
    "waldur_mastermind.matrix_chat.tasks._sender_has_project_access",
    return_value=mock.Mock(),
)
@mock.patch("waldur_mastermind.matrix_chat.matrix_client.send_reply")
class EventFilteringTest(test.APITestCase):
    @mock.patch("waldur_mastermind.matrix_chat.tasks.handle_bot_command.delay")
    def test_bot_command_detected(self, mock_handle, mock_send_reply, mock_access):
        events = [
            {
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "!help"},
                "sender": "@user:matrix.example.com",
                "room_id": "!room:matrix.example.com",
                "event_id": "$evt1",
            }
        ]
        tasks.process_appservice_events("txn-cmd", events)
        mock_handle.assert_called_once_with(
            "!room:matrix.example.com",
            "@user:matrix.example.com",
            "$evt1",
            "help",
        )

    @mock.patch("waldur_mastermind.matrix_chat.tasks.handle_bot_command.delay")
    def test_non_command_ignored(self, mock_handle, mock_send_reply, mock_access):
        events = [
            {
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "Hello everyone"},
                "sender": "@user:matrix.example.com",
                "room_id": "!room:matrix.example.com",
                "event_id": "$evt2",
            }
        ]
        tasks.process_appservice_events("txn-no-cmd", events)
        mock_handle.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.tasks.handle_bot_command.delay")
    def test_bot_own_messages_ignored(self, mock_handle, mock_send_reply, mock_access):
        events = [
            {
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "!help"},
                "sender": BOT_USER_ID,
                "room_id": "!room:matrix.example.com",
                "event_id": "$evt3",
            }
        ]
        tasks.process_appservice_events("txn-self", events)
        mock_handle.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.tasks.handle_bot_command.delay")
    def test_non_text_message_ignored(self, mock_handle, mock_send_reply, mock_access):
        events = [
            {
                "type": "m.room.message",
                "content": {"msgtype": "m.image", "body": "photo.jpg"},
                "sender": "@user:matrix.example.com",
                "room_id": "!room:matrix.example.com",
                "event_id": "$evt4",
            }
        ]
        tasks.process_appservice_events("txn-img", events)
        mock_handle.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.tasks.handle_bot_command.delay")
    def test_non_message_event_ignored(self, mock_handle, mock_send_reply, mock_access):
        events = [
            {
                "type": "m.room.member",
                "content": {"membership": "join"},
                "sender": "@user:matrix.example.com",
                "room_id": "!room:matrix.example.com",
                "event_id": "$evt5",
            }
        ]
        tasks.process_appservice_events("txn-member", events)
        mock_handle.assert_not_called()


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class CommandHandlerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room

    def test_help_command(self):
        result = tasks._cmd_help(self.room.room_id, "@user:test", "$evt")
        self.assertIn("!help", result)
        self.assertIn("!status", result)
        self.assertIn("!orders", result)
        self.assertIn("!members", result)

    def test_status_command_surfaces_errored_resource(self):
        # _cmd_status now reports problems rather than per-state counts: an
        # ERRED resource must show up in the output by name.
        resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            state=3,  # ERRED
        )
        result = tasks._cmd_status(self.room.room_id, "@user:test", "$evt")
        self.assertIn(self.fixture.project.name, result)
        self.assertIn("errored", result)
        self.assertIn(resource.name, result)

    def test_status_command_healthy_project_is_all_clear(self):
        # A project with only OK resources has nothing to surface, so the
        # status command reports the project as all clear.
        marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            state=2,  # OK
        )
        result = tasks._cmd_status(self.room.room_id, "@user:test", "$evt")
        self.assertIn(self.fixture.project.name, result)
        self.assertIn("all clear", result)

    def test_status_command_no_resources(self):
        result = tasks._cmd_status(self.room.room_id, "@user:test", "$evt")
        self.assertIn("all clear", result)

    def test_status_command_unlinked_room(self):
        result = tasks._cmd_status("!unknown:test", "@user:test", "$evt")
        self.assertIn("not linked", result)

    def test_orders_command_with_orders(self):
        marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=3,  # done
        )
        result = tasks._cmd_orders(self.room.room_id, "@user:test", "$evt")
        self.assertIn(self.fixture.project.name, result)
        self.assertIn("done", result)

    def test_orders_command_no_orders(self):
        result = tasks._cmd_orders(self.room.room_id, "@user:test", "$evt")
        self.assertIn("no orders", result)

    def test_members_command(self):
        self.fixture.matrix_room_member  # ensure member exists
        result = tasks._cmd_members(self.room.room_id, "@user:test", "$evt")
        self.assertIn("Room members", result)
        self.assertIn(self.fixture.admin.username, result)

    def test_members_command_lists_bot_when_no_db_members(self):
        # The appservice bot is always joined to an active room but isn't
        # tracked as a MatrixRoomMember, so it's prepended to the listing.
        result = tasks._cmd_members(self.room.room_id, "@user:test", "$evt")
        self.assertIn("Room members", result)
        self.assertIn("@waldur-bot:matrix.example.com", result)
        self.assertIn("bot", result)

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.send_reply")
    def test_unknown_command_returns_help(self, mock_reply):
        tasks.handle_bot_command(
            self.room.room_id,
            "@user:test",
            "$evt",
            "foobar",
        )
        mock_reply.assert_called_once()
        reply_text = mock_reply.call_args[0][2]
        self.assertIn("Unknown command", reply_text)
        self.assertIn("!help", reply_text)

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.send_reply")
    def test_handle_bot_command_calls_send_reply(self, mock_reply):
        tasks.handle_bot_command(
            self.room.room_id,
            "@user:test",
            "$evt",
            "help",
        )
        mock_reply.assert_called_once()
        reply_text = mock_reply.call_args[0][2]
        self.assertIn("Available commands", reply_text)


SETUP_URL = "/api/admin/matrix-appservice/setup/"
STATUS_URL = "/api/admin/matrix-appservice/status/"


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_USER_REGISTRATION_SECRET="test-registration-secret",
    MATRIX_APPSERVICE_AS_TOKEN="",
    MATRIX_APPSERVICE_HS_TOKEN="",
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
# The setup view tries to provision the appservice bot user on the
# homeserver at the end of the flow. Without a real homeserver listening
# at matrix.example.com, that call hangs on CI runners that drop
# (rather than refuse) outbound TCP. Stub it across the whole class —
# bot autoprovision is covered by the integration suite.
@mock.patch("waldur_mastermind.matrix_chat.views.matrix_client.ensure_bot_user_exists")
class AppserviceSetupTest(test.APITestCase):
    def setUp(self):
        from waldur_core.structure.tests.factories import UserFactory

        self.staff = UserFactory(is_staff=True)
        self.non_staff = UserFactory()

    def test_staff_can_setup(self, mock_ensure):
        self.client.force_authenticate(self.staff)
        response = self.client.post(SETUP_URL, data={}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("registration_yaml", response.data)
        self.assertIn("as_token", response.data)
        self.assertIn("hs_token", response.data)
        self.assertIn("sender_localpart", response.data)
        self.assertIn("webhook_url", response.data)

    def test_non_staff_gets_403(self, mock_ensure):
        self.client.force_authenticate(self.non_staff)
        response = self.client.post(SETUP_URL, data={}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_gets_401(self, mock_ensure):
        response = self.client.post(SETUP_URL, data={}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_tokens_auto_generated_on_first_call(self, mock_ensure):
        self.client.force_authenticate(self.staff)
        response = self.client.post(SETUP_URL, data={}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data["as_token"]) == 64)  # 32 bytes hex
        self.assertTrue(len(response.data["hs_token"]) == 64)

    def test_second_call_rotates_tokens(self, mock_ensure):
        """Re-running setup always issues fresh AS/HS tokens. The dialog warns
        the admin about this; the homeserver YAML must be updated each time."""
        self.client.force_authenticate(self.staff)
        response1 = self.client.post(SETUP_URL, data={}, format="json")
        as_token_1 = response1.data["as_token"]
        hs_token_1 = response1.data["hs_token"]

        response2 = self.client.post(SETUP_URL, data={}, format="json")
        self.assertNotEqual(response2.data["as_token"], as_token_1)
        self.assertNotEqual(response2.data["hs_token"], hs_token_1)
        self.assertEqual(len(response2.data["as_token"]), 64)
        self.assertEqual(len(response2.data["hs_token"]), 64)

    def test_custom_url_and_sender_localpart(self, mock_ensure):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            SETUP_URL,
            data={
                "url": "https://waldur.example.com",
                "sender_localpart": "my-bot",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["sender_localpart"], "my-bot")
        self.assertIn("https://waldur.example.com", response.data["webhook_url"])

    def test_response_contains_valid_yaml(self, mock_ensure):
        import yaml

        self.client.force_authenticate(self.staff)
        response = self.client.post(SETUP_URL, data={}, format="json")
        parsed = yaml.safe_load(response.data["registration_yaml"])
        self.assertIn("as_token", parsed)
        self.assertIn("hs_token", parsed)
        self.assertIn("sender_localpart", parsed)
        self.assertIn("namespaces", parsed)

    def test_serializer_accepts_homeserver_prereq_fields(self, mock_ensure):
        """The setup endpoint accepts homeserver_url, homeserver_domain, user_registration_secret in body."""
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            SETUP_URL,
            data={
                "homeserver_url": "https://matrix.example.com",
                "homeserver_domain": "matrix.example.com",
                "user_registration_secret": "shared-secret-value",
            },
            format="json",
        )
        # With prereqs already overridden in @override_config, this should 200.
        # The test asserts the serializer doesn't 400 on the new keys.
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_registration_yaml_namespace_regex_includes_domain(self, mock_ensure):
        """Regression: setup must never emit a YAML with an empty domain in the namespace regex."""
        import yaml as yaml_lib

        self.client.force_authenticate(self.staff)
        response = self.client.post(SETUP_URL, data={}, format="json")
        parsed = yaml_lib.safe_load(response.data["registration_yaml"])
        user_namespaces = parsed["namespaces"]["users"]
        # At least one namespace regex must end with the configured domain.
        domain_regexes = [
            ns["regex"]
            for ns in user_namespaces
            if ns["regex"].endswith(":matrix.example.com")
        ]
        self.assertTrue(
            domain_regexes,
            f"No namespace regex contains the configured domain. Namespaces: {user_namespaces}",
        )
        # And explicitly: no regex ends with a bare ":" (the malformed empty-domain case).
        for ns in user_namespaces:
            self.assertFalse(
                ns["regex"].endswith(":"),
                f"Namespace regex ends with bare ':' — domain was empty. Regex: {ns['regex']}",
            )

    def test_alias_namespace_lets_the_bot_create_room_aliases(self, mock_ensure):
        """Without this the homeserver answers M_EXCLUSIVE to every alias request.

        Room creation asks for `#waldur-<project>:<domain>`, catches the refusal
        and creates the room without an alias, so the failure is invisible: the
        alias column is empty forever and the "Open in Matrix client" link, which
        only renders when an alias exists, never appears.
        """
        self.client.force_authenticate(self.staff)
        response = self.client.post(SETUP_URL, data={}, format="json")
        parsed = yaml.safe_load(response.data["registration_yaml"])

        alias_namespaces = parsed["namespaces"]["aliases"]
        self.assertTrue(
            alias_namespaces,
            "Alias namespace is empty, so the appservice cannot claim any alias.",
        )
        # The alias room creation actually asks for, built the same way as
        # tasks.create_room does it.
        wanted = "#waldur-0123abcd:matrix.example.com"
        self.assertTrue(
            any(re.fullmatch(ns["regex"], wanted) for ns in alias_namespaces),
            f"No alias namespace matches {wanted}: {alias_namespaces}",
        )

    def test_alias_namespace_does_not_let_dots_match_anything(self, mock_ensure):
        """An unescaped domain makes `matrix.example.com` match `matrixXexample.com`,
        widening an exclusive claim onto homeservers we do not own."""
        self.client.force_authenticate(self.staff)
        response = self.client.post(SETUP_URL, data={}, format="json")
        parsed = yaml.safe_load(response.data["registration_yaml"])

        for ns in parsed["namespaces"]["aliases"]:
            self.assertIsNone(
                re.fullmatch(ns["regex"], "#waldur-0123abcd:matrixXexample.com"),
                f"Regex {ns['regex']} matches a domain we do not own.",
            )

    def test_bot_namespace_is_exclusive_and_domain_scoped(self, mock_ensure):
        """Bot user namespace must be scoped to the configured domain and claimed exclusively.

        Why: scoping prevents the bot identity from being registered through normal
        signup on the local homeserver. Loose `.*` for the bot's domain serves no
        purpose since the bot always lives on MATRIX_HOMESERVER_DOMAIN.
        """
        import yaml as yaml_lib

        self.client.force_authenticate(self.staff)
        response = self.client.post(SETUP_URL, data={}, format="json")
        parsed = yaml_lib.safe_load(response.data["registration_yaml"])
        user_namespaces = parsed["namespaces"]["users"]

        bot_rules = [
            ns
            for ns in user_namespaces
            if ns["regex"] == "@waldur-bot:matrix.example.com"
        ]
        self.assertEqual(
            len(bot_rules),
            1,
            f"Expected exactly one bot namespace rule scoped to the domain. Got: {user_namespaces}",
        )
        self.assertTrue(
            bot_rules[0]["exclusive"],
            "Bot namespace must be exclusive so no other registration can claim the bot identity.",
        )

        # The wildcard user rule must stay non-exclusive — otherwise normal client
        # signups on the local homeserver would be blocked.
        local_user_rules = [
            ns for ns in user_namespaces if ns["regex"] == "@.*:matrix.example.com"
        ]
        self.assertEqual(len(local_user_rules), 1)
        self.assertFalse(
            local_user_rules[0]["exclusive"],
            "Wildcard user namespace must NOT be exclusive — would block normal signups.",
        )

    def test_cli_command_claims_the_same_namespaces_as_setup(self, mock_ensure):
        """Both hand operators a registration to install on the homeserver.

        While each assembled its own, a namespace fix could reach one and not
        the other: the alias namespace landed in Setup while the command kept
        declaring none.
        """
        self.client.force_authenticate(self.staff)
        response = self.client.post(SETUP_URL, data={}, format="json")
        from_setup = yaml.safe_load(response.data["registration_yaml"])

        out = io.StringIO()
        call_command("generate_appservice_registration", stdout=out)
        from_command = yaml.safe_load(out.getvalue())

        self.assertEqual(from_command["namespaces"], from_setup["namespaces"])


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="",
    MATRIX_HOMESERVER_DOMAIN="",
    MATRIX_USER_REGISTRATION_SECRET="",
    MATRIX_APPSERVICE_AS_TOKEN="",
    MATRIX_APPSERVICE_HS_TOKEN="",
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
# See AppserviceSetupTest for the rationale — bot autoprovision hits a
# real homeserver and stalls on CI runners without one.
@mock.patch("waldur_mastermind.matrix_chat.views.matrix_client.ensure_bot_user_exists")
class AppserviceSetupFirstRunTest(test.APITestCase):
    def setUp(self):
        from waldur_core.structure.tests.factories import UserFactory

        self.staff = UserFactory(is_staff=True)

    def test_setup_rejects_when_all_prereqs_missing_and_none_provided(
        self, mock_ensure
    ):
        self.client.force_authenticate(self.staff)
        response = self.client.post(SETUP_URL, data={}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        detail = response.data.get("detail", "")
        self.assertIn("MATRIX_HOMESERVER_URL", str(detail))
        self.assertIn("MATRIX_HOMESERVER_DOMAIN", str(detail))
        self.assertIn("MATRIX_USER_REGISTRATION_SECRET", str(detail))

    def test_setup_persists_provided_prereqs_and_generates_yaml(self, mock_ensure):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            SETUP_URL,
            data={
                "homeserver_url": "https://matrix.example.com",
                "homeserver_domain": "matrix.example.com",
                "user_registration_secret": "supersecret",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("registration_yaml", response.data)

        # All three prereqs are now persisted to Constance.
        from constance import config as live_config

        self.assertEqual(
            live_config.MATRIX_HOMESERVER_URL, "https://matrix.example.com"
        )
        self.assertEqual(live_config.MATRIX_HOMESERVER_DOMAIN, "matrix.example.com")
        self.assertEqual(live_config.MATRIX_USER_REGISTRATION_SECRET, "supersecret")

    def test_setup_persists_only_missing_prereqs_in_partial_request(self, mock_ensure):
        """When URL is already configured, only the missing prereqs need to be in the body."""
        # Use override_config (not direct setattr) so cache invalidation and
        # restoration are handled correctly across tests.
        with override_config(MATRIX_HOMESERVER_URL="https://pre-existing.example.com"):
            self.client.force_authenticate(self.staff)
            response = self.client.post(
                SETUP_URL,
                data={
                    "homeserver_domain": "pre-existing.example.com",
                    "user_registration_secret": "secret-x",
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            from constance import config as live_config

            # URL was not in the request body — the override-managed value is preserved.
            self.assertEqual(
                live_config.MATRIX_HOMESERVER_URL,
                "https://pre-existing.example.com",
            )
            self.assertEqual(
                live_config.MATRIX_HOMESERVER_DOMAIN, "pre-existing.example.com"
            )
            self.assertEqual(live_config.MATRIX_USER_REGISTRATION_SECRET, "secret-x")

    def test_setup_does_not_overwrite_already_configured_prereq(self, mock_ensure):
        """A prereq supplied in the body is ignored when Constance already has it."""
        with override_config(MATRIX_HOMESERVER_URL="https://pre-existing.example.com"):
            self.client.force_authenticate(self.staff)
            response = self.client.post(
                SETUP_URL,
                data={
                    "homeserver_url": "https://body-supplied.example.com",
                    "homeserver_domain": "pre-existing.example.com",
                    "user_registration_secret": "secret-x",
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            from constance import config as live_config

            # The pre-configured value wins; the body value is dropped.
            self.assertEqual(
                live_config.MATRIX_HOMESERVER_URL,
                "https://pre-existing.example.com",
            )

    def test_setup_persists_optional_public_url(self, mock_ensure):
        """The optional homeserver_public_url is persisted without gating setup."""
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            SETUP_URL,
            data={
                "homeserver_url": "http://tuwunel.internal:6167",
                "homeserver_public_url": "https://waldur.example.com",
                "homeserver_domain": "waldur.example.com",
                "user_registration_secret": "supersecret",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        from constance import config as live_config

        self.assertEqual(
            live_config.MATRIX_HOMESERVER_URL, "http://tuwunel.internal:6167"
        )
        self.assertEqual(
            live_config.MATRIX_HOMESERVER_PUBLIC_URL, "https://waldur.example.com"
        )


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class AppserviceStatusTest(test.APITestCase):
    def setUp(self):
        from waldur_core.structure.tests.factories import UserFactory

        self.staff = UserFactory(is_staff=True)
        self.non_staff = UserFactory()

    def test_staff_can_get_status(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(STATUS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["enabled"])
        self.assertTrue(response.data["as_token_configured"])
        self.assertTrue(response.data["hs_token_configured"])
        self.assertEqual(response.data["sender_localpart"], "waldur-bot")
        self.assertEqual(response.data["bot_user_id"], "@waldur-bot:matrix.example.com")
        # Must be the Matrix appservice spec v1 path — the homeserver appends
        # this to the registration's `url:` field.
        self.assertEqual(
            response.data["webhook_path"], "/_matrix/app/v1/transactions/{txnId}"
        )
        self.assertEqual(response.data["homeserver_url"], "https://matrix.example.com")
        self.assertEqual(response.data["homeserver_domain"], "matrix.example.com")
        self.assertEqual(response.data["transaction_count"], 0)

    def test_non_staff_gets_403(self):
        self.client.force_authenticate(self.non_staff)
        response = self.client.get(STATUS_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_gets_401(self):
        response = self.client.get(STATUS_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_transaction_count_reflects_records(self):
        models.MatrixAppserviceTransaction.objects.create(
            txn_id="txn-status-1", event_count=3
        )
        models.MatrixAppserviceTransaction.objects.create(
            txn_id="txn-status-2", event_count=5
        )
        self.client.force_authenticate(self.staff)
        response = self.client.get(STATUS_URL)
        self.assertEqual(response.data["transaction_count"], 2)

    @override_config(
        MATRIX_APPSERVICE_AS_TOKEN="",
        MATRIX_APPSERVICE_HS_TOKEN="",
    )
    def test_disabled_state(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(STATUS_URL)
        self.assertFalse(response.data["enabled"])
        self.assertFalse(response.data["as_token_configured"])
        self.assertFalse(response.data["hs_token_configured"])

    @override_config(
        MATRIX_HOMESERVER_PUBLIC_URL="https://public.example.com",
    )
    def test_status_returns_public_url_when_set(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(STATUS_URL)
        # Public URL overrides the internal one in browser-facing responses.
        self.assertEqual(response.data["homeserver_url"], "https://public.example.com")


DIAGNOSTICS_URL = "/api/admin/matrix/diagnostics/"


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="http://tuwunel.internal:6167",
    MATRIX_HOMESERVER_DOMAIN="waldur.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_USER_REGISTRATION_SECRET="reg-secret",
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class AppserviceDiagnosticsTest(test.APITestCase):
    def setUp(self):
        from waldur_core.structure.tests.factories import UserFactory

        self.staff = UserFactory(is_staff=True)

    @mock.patch("waldur_mastermind.matrix_chat.views.httpx.get")
    def test_diagnostics_includes_public_url_checks(self, mock_httpx_get):
        # Both probes return OK so we can isolate the new check entries.
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"versions": ["v1.16"], "server": {}}
        mock_httpx_get.return_value = mock_resp

        with override_config(MATRIX_HOMESERVER_PUBLIC_URL="https://waldur.example.com"):
            self.client.force_authenticate(self.staff)
            response = self.client.get(DIAGNOSTICS_URL)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            names = {c["name"] for c in response.data["checks"]}
            self.assertIn("public_homeserver_configured", names)
            self.assertIn("public_homeserver_reachable", names)

            by_name = {c["name"]: c for c in response.data["checks"]}
            self.assertTrue(by_name["public_homeserver_configured"]["ok"])
            self.assertIn(
                "https://waldur.example.com",
                by_name["public_homeserver_configured"]["detail"],
            )
            self.assertTrue(by_name["public_homeserver_reachable"]["ok"])

    @mock.patch("waldur_mastermind.matrix_chat.views.httpx.get")
    def test_diagnostics_skips_public_probe_when_same_as_internal(self, mock_httpx_get):
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"versions": ["v1.16"], "server": {}}
        mock_httpx_get.return_value = mock_resp

        # No MATRIX_HOMESERVER_PUBLIC_URL override — falls back to internal.
        self.client.force_authenticate(self.staff)
        response = self.client.get(DIAGNOSTICS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        by_name = {c["name"]: c for c in response.data["checks"]}
        self.assertIn(
            "Same as internal", by_name["public_homeserver_reachable"]["detail"]
        )

    @staticmethod
    def _well_known_with_livekit(url, **kwargs):
        # Drives httpx.get per URL: the .well-known returns an MSC4143 LiveKit
        # focus; every other probe (versions, whoami, joined_rooms, the SFU
        # itself) answers 200 so we can isolate the LiveKit check entries.
        resp = mock.MagicMock()
        resp.status_code = 200
        if url.endswith("/.well-known/matrix/client"):
            resp.json.return_value = {
                "org.matrix.msc4143.rtc_foci": [
                    {
                        "type": "livekit",
                        "livekit_service_url": "https://lk.example.com",
                    }
                ]
            }
        else:
            resp.json.return_value = {"versions": ["v1.16"], "server": {}}
        return resp

    @mock.patch("waldur_mastermind.matrix_chat.views.httpx.get")
    def test_diagnostics_discovers_livekit_from_well_known(self, mock_httpx_get):
        mock_httpx_get.side_effect = self._well_known_with_livekit

        self.client.force_authenticate(self.staff)
        response = self.client.get(DIAGNOSTICS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        by_name = {c["name"]: c for c in response.data["checks"]}
        self.assertTrue(by_name["livekit_configured"]["ok"])
        self.assertIn("https://lk.example.com", by_name["livekit_configured"]["detail"])

    @mock.patch("waldur_mastermind.matrix_chat.views.httpx.get")
    def test_diagnostics_flags_missing_livekit_focus(self, mock_httpx_get):
        # .well-known has no rtc_foci at all.
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"versions": ["v1.16"], "server": {}}
        mock_httpx_get.return_value = mock_resp

        self.client.force_authenticate(self.staff)
        response = self.client.get(DIAGNOSTICS_URL)

        by_name = {c["name"]: c for c in response.data["checks"]}
        self.assertFalse(by_name["livekit_configured"]["ok"])
        self.assertIn("No LiveKit focus", by_name["livekit_configured"]["detail"])


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="http://tuwunel.internal:6167",
    MATRIX_HOMESERVER_DOMAIN="waldur.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class HomeserverVersionDiagnosticsTest(test.APITestCase):
    VERSIONS = {"versions": ["r0.6.1", "v1.9", "v1.10", "v1.2"]}

    def _detail(self, answers):
        # `answers` maps a URL path to (status, body); anything else answers
        # 200 with the spec versions.
        def get(url, **kwargs):
            path = url.removeprefix("http://tuwunel.internal:6167")
            status_code, body = answers.get(path, (200, self.VERSIONS))
            response = mock.MagicMock()
            response.status_code = status_code
            response.json.return_value = body
            return response

        with mock.patch(
            "waldur_mastermind.matrix_chat.views.httpx.get", side_effect=get
        ):
            self.client.force_authenticate(fixtures.MatrixChatFixture().staff)
            response = self.client.get(DIAGNOSTICS_URL)
        by_name = {c["name"]: c for c in response.data["checks"]}
        return by_name["homeserver_reachable"]["detail"]

    def test_names_the_homeserver_and_its_newest_spec_version(self):
        detail = self._detail(
            {
                "/_matrix/federation/v1/version": (
                    200,
                    {"server": {"name": "Tuwunel", "version": "1.9.0"}},
                )
            }
        )

        # v1.10 is newer than v1.9, whatever order the homeserver lists them in.
        self.assertEqual(detail, "OK — Tuwunel 1.9.0, Matrix up to v1.10")

    def test_asks_tuwunel_when_federation_is_switched_off(self):
        detail = self._detail(
            {
                "/_matrix/federation/v1/version": (
                    403,
                    {"errcode": "M_FORBIDDEN", "error": "Federation is disabled."},
                ),
                "/_tuwunel/server_version": (
                    200,
                    {"name": "Tuwunel", "version": "1.9.0"},
                ),
            }
        )

        self.assertEqual(detail, "OK — Tuwunel 1.9.0, Matrix up to v1.10")

    def test_leaves_the_name_out_when_no_endpoint_gives_it(self):
        detail = self._detail(
            {
                "/_matrix/federation/v1/version": (404, {}),
                "/_tuwunel/server_version": (404, {}),
            }
        )

        self.assertEqual(detail, "OK — Matrix up to v1.10")


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="http://tuwunel.internal:6167",
    MATRIX_HOMESERVER_DOMAIN="waldur.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class BotAuthenticationDiagnosticsTest(test.APITestCase):
    def _checks(self, joined_rooms_status=200):
        def get(url, **kwargs):
            response = mock.MagicMock()
            response.status_code = 200
            if url.endswith("/account/whoami"):
                response.json.return_value = {"user_id": BOT_USER_ID}
            elif url.endswith("/joined_rooms"):
                response.status_code = joined_rooms_status
                response.json.return_value = {"joined_rooms": ["!a:x", "!b:x"]}
            else:
                response.json.return_value = {"versions": ["v1.16"]}
            return response

        with mock.patch(
            "waldur_mastermind.matrix_chat.views.httpx.get", side_effect=get
        ):
            self.client.force_authenticate(fixtures.MatrixChatFixture().staff)
            response = self.client.get(DIAGNOSTICS_URL)
        return {c["name"]: c for c in response.data["checks"]}

    def test_reports_the_bot_and_its_rooms_in_one_row(self):
        # Listing rooms uses the same token as whoami, so a row of its own only
        # repeated this one.
        checks = self._checks()

        self.assertTrue(checks["bot_whoami"]["ok"])
        self.assertEqual(
            checks["bot_whoami"]["detail"],
            f"OK — authenticated as {BOT_USER_ID}, in 2 room(s)",
        )
        self.assertNotIn("bot_functional", checks)

    def test_says_so_when_the_rooms_cannot_be_listed(self):
        checks = self._checks(joined_rooms_status=500)

        self.assertTrue(checks["bot_whoami"]["ok"])
        self.assertEqual(
            checks["bot_whoami"]["detail"],
            f"OK — authenticated as {BOT_USER_ID}; could not list its rooms (HTTP 500)",
        )


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="http://tuwunel.internal:6167",
    MATRIX_HOMESERVER_DOMAIN="waldur.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class AppserviceUserNamespaceDiagnosticsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.staff = self.fixture.staff
        self.acted_as = []

    def _response(self, status_code, body):
        response = mock.MagicMock()
        response.status_code = status_code
        response.json.return_value = body
        response.text = json.dumps(body)
        return response

    def _homeserver(self, user_status=200, user_body=None, bot_status=200):
        # Answers whoami as a user per the arguments, and every other probe OK.
        def get(url, params=None, **kwargs):
            if url.endswith("/account/whoami") and params:
                self.acted_as.append(params["user_id"])
                return self._response(user_status, user_body or {})
            if url.endswith("/account/whoami"):
                return self._response(bot_status, {"user_id": BOT_USER_ID})
            return self._response(200, {"versions": ["v1.16"], "joined_rooms": []})

        return get

    def _check(self, **homeserver):
        with mock.patch(
            "waldur_mastermind.matrix_chat.views.httpx.get",
            side_effect=self._homeserver(**homeserver),
        ):
            self.client.force_authenticate(self.staff)
            response = self.client.get(DIAGNOSTICS_URL)
        by_name = {c["name"]: c for c in response.data["checks"]}
        return by_name["appservice_user_namespace"]

    def test_passes_when_the_appservice_can_act_for_users(self):
        check = self._check()

        self.assertTrue(check["ok"])
        # The ID Waldur would give the staff user; it need not exist yet.
        self.assertEqual(self.acted_as, [f"@{self.staff.username}:waldur.example.com"])

    def test_acts_as_the_provisioned_id_when_there_is_one(self):
        models.MatrixUserProfile.objects.create(
            user=self.staff,
            matrix_user_id="@an-older-id:waldur.example.com",
            provisioned=True,
        )

        self._check()

        self.assertEqual(self.acted_as, ["@an-older-id:waldur.example.com"])

    def test_fails_when_the_registration_does_not_cover_users(self):
        # Tuwunel's answer for a user outside the appservice's namespace.
        check = self._check(
            user_status=400,
            user_body={
                "errcode": "M_EXCLUSIVE",
                "error": "M_EXCLUSIVE: User is not in namespace.",
            },
        )

        self.assertFalse(check["ok"])
        self.assertIn("namespace", check["detail"])
        self.assertIn("register", check["detail"])

    def test_reports_other_errors_as_they_are(self):
        check = self._check(user_status=502, user_body={"error": "bad gateway"})

        self.assertFalse(check["ok"])
        self.assertIn("502", check["detail"])

    def test_is_skipped_when_the_bot_cannot_authenticate(self):
        check = self._check(bot_status=401)

        self.assertFalse(check["ok"])
        self.assertIn("Skipped", check["detail"])
        self.assertEqual(self.acted_as, [])

    def test_counts_room_members_left_as_invited(self):
        member = self.fixture.matrix_room_member
        member.membership_state = models.MembershipStates.INVITED
        member.save(update_fields=["membership_state"])

        check = self._check(
            user_status=400,
            user_body={"errcode": "M_EXCLUSIVE", "error": "User is not in namespace."},
        )

        self.assertIn("1 room member", check["detail"])


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="http://tuwunel.internal:6167",
    MATRIX_HOMESERVER_DOMAIN="waldur.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class UserProfilesDiagnosticsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room

    def _check(self):
        response = mock.MagicMock()
        response.status_code = 200
        response.json.return_value = {"versions": ["v1.16"]}
        with mock.patch(
            "waldur_mastermind.matrix_chat.views.httpx.get", return_value=response
        ):
            self.client.force_authenticate(self.fixture.staff)
            response = self.client.get(DIAGNOSTICS_URL)
        by_name = {c["name"]: c for c in response.data["checks"]}
        return by_name["user_stats"]

    def test_fails_naming_room_members_without_a_profile(self):
        # A project member and a customer owner belong in the room; the
        # admin has a profile.
        self.fixture.matrix_user_profile
        names = sorted([self.fixture.owner.username, self.fixture.manager.username])

        check = self._check()

        self.assertFalse(check["ok"])
        self.assertEqual(
            check["detail"],
            "1 provisioned out of 1 total; "
            f"2 room member(s) without a Matrix account: {', '.join(names)}. "
            "The worker log says why; link an existing account with "
            "`waldur link_matrix_account <user> <matrix id>`",
        )

    def test_leaves_out_users_member_sync_would_not_add(self):
        # A customer role that runs no chat, a deactivated member, and a
        # project whose room is archived.
        self.fixture.customer_support
        self.fixture.member.is_active = False
        self.fixture.member.save(update_fields=["is_active"])
        self.room.state = models.RoomStates.ARCHIVED
        self.room.save(update_fields=["state"])
        self.fixture.admin

        check = self._check()

        self.assertTrue(check["ok"])
        self.assertEqual(check["detail"], "0 provisioned out of 0 total")

    def test_names_ten_and_counts_the_rest(self):
        # The room's creator, the customer owner, is linked.
        models.MatrixUserProfile.objects.create(
            user=self.fixture.owner,
            matrix_user_id=f"@{self.fixture.owner.username}:waldur.example.com",
        )
        users = [UserFactory() for _ in range(12)]
        for user in users:
            add_user_to_project(user, self.fixture.project, ProjectRole.MEMBER)
        shown = sorted(u.username for u in users)[:10]

        check = self._check()

        self.assertIn("; 12 room member(s) without a Matrix account: ", check["detail"])
        self.assertIn(f"{', '.join(shown)} and 2 more.", check["detail"])


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN=AS_TOKEN,
    MATRIX_APPSERVICE_HS_TOKEN=HS_TOKEN,
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
)
class WebhookRejectionLogTest(test.APITestCase):
    def _put(self, **headers):
        with self.assertLogs("waldur_mastermind.matrix_chat.views", "WARNING") as logs:
            response = self.client.put(
                f"{WEBHOOK_URL}txn-rejected",
                data={"events": []},
                format="json",
                **headers,
            )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        return "\n".join(logs.output)

    def test_a_wrong_token_is_logged_without_either_token(self):
        log = self._put(HTTP_AUTHORIZATION="Bearer wrong-token")

        self.assertIn(f"{WEBHOOK_URL}txn-rejected", log)
        self.assertIn("MATRIX_APPSERVICE_HS_TOKEN", log)
        self.assertNotIn("wrong-token", log)
        self.assertNotIn(HS_TOKEN, log)

    def test_a_missing_token_is_logged(self):
        log = self._put()

        self.assertIn("no token", log)

    def test_an_unset_hs_token_is_logged(self):
        with override_config(MATRIX_APPSERVICE_HS_TOKEN=""):
            log = self._put(HTTP_AUTHORIZATION=f"Bearer {HS_TOKEN}")

        self.assertIn("not set", log)
