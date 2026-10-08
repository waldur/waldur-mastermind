import json
from unittest import mock

import httpx
import respx
from constance.test import override_config
from django.test import TestCase
from nio import (
    CallHangupEvent,
    ReactionEvent,
    RedactedEvent,
    RedactionEvent,
    RoomGetStateEventResponse,
    RoomMessageImage,
    RoomMessageText,
    RoomNameEvent,
    RoomPutStateResponse,
    RoomTopicEvent,
    StickerEvent,
)

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import matrix_client


class GenerateMatrixUserIdTest(TestCase):
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_username_format(self, mock_config):
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_USER_ID_FORMAT = "username"
        user = structure_factories.UserFactory(username="alice")
        result = matrix_client.generate_matrix_user_id(user)
        self.assertEqual(result, "@alice:matrix.example.com")

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_uuid_format(self, mock_config):
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_USER_ID_FORMAT = "uuid"
        user = structure_factories.UserFactory()
        result = matrix_client.generate_matrix_user_id(user)
        expected_localpart = str(user.uuid).replace("-", "")
        self.assertEqual(result, f"@{expected_localpart}:matrix.example.com")

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_email_local_format(self, mock_config):
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_USER_ID_FORMAT = "email_local"
        user = structure_factories.UserFactory(email="bob@corp.com")
        result = matrix_client.generate_matrix_user_id(user)
        self.assertEqual(result, "@bob:matrix.example.com")

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_email_local_format_no_email(self, mock_config):
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_USER_ID_FORMAT = "email_local"
        user = structure_factories.UserFactory(email="", username="fallback_user")
        result = matrix_client.generate_matrix_user_id(user)
        self.assertEqual(result, "@fallback_user:matrix.example.com")

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_sanitization_of_localpart(self, mock_config):
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_USER_ID_FORMAT = "username"
        user = structure_factories.UserFactory(username="Alice Smith!")
        result = matrix_client.generate_matrix_user_id(user)
        self.assertEqual(result, "@alice_smith_:matrix.example.com")


class IsEnabledTest(TestCase):
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_enabled_when_all_configured(self, mock_config):
        mock_config.MATRIX_ENABLED = True
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"
        self.assertTrue(matrix_client.is_enabled())

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_disabled_when_flag_false(self, mock_config):
        mock_config.MATRIX_ENABLED = False
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"
        self.assertFalse(matrix_client.is_enabled())

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_disabled_when_url_empty(self, mock_config):
        mock_config.MATRIX_ENABLED = True
        mock_config.MATRIX_HOMESERVER_URL = ""
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"
        self.assertFalse(matrix_client.is_enabled())

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_disabled_when_as_token_empty(self, mock_config):
        mock_config.MATRIX_ENABLED = True
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = ""
        self.assertFalse(matrix_client.is_enabled())

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_homeserver_stays_configured_when_flag_false(self, mock_config):
        # Revocations still need the homeserver after chat is switched off.
        mock_config.MATRIX_ENABLED = False
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"
        self.assertTrue(matrix_client.is_homeserver_configured())

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_homeserver_not_configured_without_as_token(self, mock_config):
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = ""
        self.assertFalse(matrix_client.is_homeserver_configured())


class EnsureUserExistsTest(TestCase):
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._run_async")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_creates_profile_and_provisions(self, mock_config, mock_run_async):
        mock_config.MATRIX_ENABLED = True
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"
        mock_config.MATRIX_APPSERVICE_SENDER_LOCALPART = "waldur-bot"
        mock_config.MATRIX_USER_ID_FORMAT = "username"
        mock_config.MATRIX_USER_REGISTRATION_SECRET = "test-secret"

        mock_run_async.return_value = {"name": "@testuser:matrix.example.com"}

        user = structure_factories.UserFactory(username="testuser")
        matrix_user_id = matrix_client.ensure_user_exists(user)

        self.assertEqual(matrix_user_id, "@testuser:matrix.example.com")
        # Called twice: once for registration, once for set_display_name
        self.assertEqual(mock_run_async.call_count, 2)

        from waldur_mastermind.matrix_chat.models import MatrixUserProfile

        profile = MatrixUserProfile.objects.get(user=user)
        self.assertTrue(profile.provisioned)
        self.assertIsNotNone(profile.provisioned_at)

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_returns_existing_provisioned_profile(self, mock_config):
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_USER_ID_FORMAT = "username"

        user = structure_factories.UserFactory(username="existinguser")
        from waldur_mastermind.matrix_chat.models import MatrixUserProfile

        MatrixUserProfile.objects.create(
            user=user,
            matrix_user_id="@existinguser:matrix.example.com",
            provisioned=True,
        )

        result = matrix_client.ensure_user_exists(user)
        self.assertEqual(result, "@existinguser:matrix.example.com")

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._run_async")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_succeeds_when_user_already_exists(self, mock_config, mock_run_async):
        mock_config.MATRIX_ENABLED = True
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"
        mock_config.MATRIX_APPSERVICE_SENDER_LOCALPART = "waldur-bot"
        mock_config.MATRIX_USER_ID_FORMAT = "username"
        mock_config.MATRIX_USER_REGISTRATION_SECRET = "test-secret"

        # _register_user_async returns None when M_USER_IN_USE
        mock_run_async.return_value = None

        user = structure_factories.UserFactory(username="duplicateuser")
        matrix_user_id = matrix_client.ensure_user_exists(user)

        self.assertEqual(matrix_user_id, "@duplicateuser:matrix.example.com")

        from waldur_mastermind.matrix_chat.models import MatrixUserProfile

        profile = MatrixUserProfile.objects.get(user=user)
        self.assertTrue(profile.provisioned)

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._run_async")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_raises_on_registration_error(self, mock_config, mock_run_async):
        mock_config.MATRIX_ENABLED = True
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"
        mock_config.MATRIX_APPSERVICE_SENDER_LOCALPART = "waldur-bot"
        mock_config.MATRIX_USER_ID_FORMAT = "username"
        mock_config.MATRIX_USER_REGISTRATION_SECRET = "test-secret"

        mock_run_async.side_effect = matrix_client.MatrixClientError(
            "Failed to register user failuser: Registration disabled"
        )

        user = structure_factories.UserFactory(username="failuser")
        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.ensure_user_exists(user)

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_raises_when_secret_not_configured(self, mock_config):
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_USER_ID_FORMAT = "username"
        mock_config.MATRIX_USER_REGISTRATION_SECRET = ""

        user = structure_factories.UserFactory(username="nosecretuser")
        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.ensure_user_exists(user)


@override_config(
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
    MATRIX_USER_REGISTRATION_SECRET="test-secret",
    MATRIX_USER_ID_FORMAT="username",
)
class RegistrationCreatesNoSessionTest(TestCase):
    """Registration only creates the account: sessions come from /session/."""

    @respx.mock
    @mock.patch.object(matrix_client, "set_display_name")
    def test_every_registration_request_inhibits_login(self, mock_display_name):
        route = respx.post(
            "https://matrix.example.com/_matrix/client/v3/register"
        ).mock(
            side_effect=[
                httpx.Response(
                    401,
                    json={
                        "session": "uia",
                        "flows": [{"stages": ["m.login.registration_token"]}],
                    },
                ),
                httpx.Response(200, json={"user_id": "@alice:matrix.example.com"}),
            ]
        )
        user = structure_factories.UserFactory(username="alice")

        matrix_client.ensure_user_exists(user)

        bodies = [json.loads(call.request.content) for call in route.calls]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(all(body["inhibit_login"] for body in bodies))

    @respx.mock
    @mock.patch.object(matrix_client, "set_display_name")
    def test_appservice_registration_inhibits_login(self, mock_display_name):
        # Without the registration-token flow, the appservice registers the user.
        route = respx.post(
            "https://matrix.example.com/_matrix/client/v3/register"
        ).mock(
            side_effect=[
                httpx.Response(401, json={"session": "uia", "flows": []}),
                httpx.Response(200, json={"user_id": "@alice:matrix.example.com"}),
            ]
        )

        matrix_client.ensure_user_exists(
            structure_factories.UserFactory(username="alice")
        )

        bodies = [json.loads(call.request.content) for call in route.calls]
        self.assertEqual(bodies[-1]["auth"]["type"], "m.login.application_service")
        self.assertTrue(all(body["inhibit_login"] for body in bodies))

    @respx.mock
    @mock.patch.object(matrix_client, "set_display_name")
    def test_dummy_registration_inhibits_login(self, mock_display_name):
        route = respx.post(
            "https://matrix.example.com/_matrix/client/v3/register"
        ).mock(
            side_effect=[
                httpx.Response(401, json={"session": "uia", "flows": []}),
                httpx.Response(400, json={"errcode": "M_EXCLUSIVE"}),
                httpx.Response(
                    401,
                    json={"session": "uia", "flows": [{"stages": ["m.login.dummy"]}]},
                ),
                httpx.Response(200, json={"user_id": "@alice:matrix.example.com"}),
            ]
        )

        matrix_client.ensure_user_exists(
            structure_factories.UserFactory(username="alice")
        )

        bodies = [json.loads(call.request.content) for call in route.calls]
        self.assertEqual(bodies[-1]["auth"]["type"], "m.login.dummy")
        self.assertTrue(all(body["inhibit_login"] for body in bodies))

    @respx.mock
    @mock.patch.object(matrix_client, "set_display_name")
    def test_bot_registration_inhibits_login(self, mock_display_name):
        route = respx.post(
            "https://matrix.example.com/_matrix/client/v3/register"
        ).mock(return_value=httpx.Response(200, json={}))

        matrix_client.ensure_bot_user_exists()

        self.assertTrue(json.loads(route.calls.last.request.content)["inhibit_login"])


class EnsureBotUserExistsTest(TestCase):
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.set_display_name")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._run_async")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_sets_bot_display_name_after_registration(
        self, mock_config, mock_run_async, mock_set_display_name
    ):
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"
        mock_config.MATRIX_APPSERVICE_SENDER_LOCALPART = "waldur-bot"
        mock_config.SITE_NAME = "Waldur"
        mock_run_async.return_value = {"name": "@waldur-bot:matrix.example.com"}

        matrix_client.ensure_bot_user_exists()

        mock_set_display_name.assert_called_once_with(
            "@waldur-bot:matrix.example.com", "Waldur Bot"
        )

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.set_display_name")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._run_async")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_sets_bot_display_name_when_bot_already_exists(
        self, mock_config, mock_run_async, mock_set_display_name
    ):
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"
        mock_config.MATRIX_APPSERVICE_SENDER_LOCALPART = "waldur-bot"
        mock_config.SITE_NAME = "Waldur"
        # Registration returns None when the bot user already exists.
        mock_run_async.return_value = None

        matrix_client.ensure_bot_user_exists()

        mock_set_display_name.assert_called_once_with(
            "@waldur-bot:matrix.example.com", "Waldur Bot"
        )

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.set_display_name")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._run_async")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.config")
    def test_bot_display_name_follows_site_name_for_whitelabel(
        self, mock_config, mock_run_async, mock_set_display_name
    ):
        mock_config.MATRIX_HOMESERVER_URL = "https://matrix.example.com"
        mock_config.MATRIX_HOMESERVER_DOMAIN = "matrix.example.com"
        mock_config.MATRIX_APPSERVICE_AS_TOKEN = "as_token_123"
        mock_config.MATRIX_APPSERVICE_SENDER_LOCALPART = "waldur-bot"
        mock_config.SITE_NAME = "Acme"
        mock_run_async.return_value = {"name": "@waldur-bot:matrix.example.com"}

        matrix_client.ensure_bot_user_exists()

        mock_set_display_name.assert_called_once_with(
            "@waldur-bot:matrix.example.com", "Acme Bot"
        )


class SendMessageTest(TestCase):
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._run_async")
    def test_send_message_success(self, mock_run_async):
        mock_run_async.return_value = "$event123"
        event_id = matrix_client.send_message("!room:example.com", "Hello world")
        self.assertEqual(event_id, "$event123")
        mock_run_async.assert_called_once()

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._run_async")
    def test_send_message_error(self, mock_run_async):
        mock_run_async.side_effect = matrix_client.MatrixClientError("Send failed")
        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.send_message("!room:example.com", "Hello world")


class BuildTextContentTest(TestCase):
    def test_plain_markdown_becomes_formatted_body(self):
        content = matrix_client.build_text_content("**Room members (2):**")
        # body stays the markdown fallback for HTML-less clients
        self.assertEqual(content["body"], "**Room members (2):**")
        self.assertEqual(content["msgtype"], "m.text")
        self.assertEqual(content["format"], "org.matrix.custom.html")
        self.assertEqual(
            content["formatted_body"], "<p><strong>Room members (2):</strong></p>"
        )

    def test_inline_code_and_lists(self):
        content = matrix_client.build_text_content(
            "**Members:**\n\n- `@waldur-bot:matrix.local` — bot"
        )
        self.assertEqual(
            content["formatted_body"],
            "<p><strong>Members:</strong></p>\n"
            "<ul>\n<li><code>@waldur-bot:matrix.local</code> — bot</li>\n</ul>",
        )

    def test_user_data_is_html_escaped(self):
        # project/display names flow into bot messages and are attacker-controlled
        content = matrix_client.build_text_content("**<script>evil</script>**")
        self.assertNotIn("<script>", content["formatted_body"])
        self.assertIn("&lt;script&gt;", content["formatted_body"])

    def test_tags_outside_allowlist_are_stripped(self):
        # markdown-it legitimately emits <img> for image syntax, but the bot's
        # formatted_body must stay within the shared clean_html allowlist
        # (defense-in-depth over raw markdown rendering). Without the sanitizer
        # pass the <img> tag survives into the message.
        content = matrix_client.build_text_content("![x](https://e.example/a.png)")
        self.assertNotIn("<img", content["formatted_body"])

    def test_msgtype_passthrough(self):
        content = matrix_client.build_text_content("hi", msgtype="m.notice")
        self.assertEqual(content["msgtype"], "m.notice")

    def test_reply_adds_relation(self):
        content = matrix_client.build_text_content("ok", reply_to="$abc")
        self.assertEqual(
            content["m.relates_to"], {"m.in_reply_to": {"event_id": "$abc"}}
        )


class BuildEventMessageTest(TestCase):
    """Tests for _build_event_message dispatching logic."""

    def _make_event(self, cls, **kwargs):
        """Create a nio event from a source dict."""
        return cls(
            kwargs.get("source", {}),
            **{k: v for k, v in kwargs.items() if k != "source"},
        )

    def test_image_message(self):
        source = {
            "type": "m.room.message",
            "event_id": "$img1",
            "sender": "@alice:example.com",
            "origin_server_ts": 1000,
            "content": {
                "msgtype": "m.image",
                "body": "photo.jpg",
                "url": "mxc://example.com/media123",
                "info": {"mimetype": "image/jpeg", "size": 12345, "w": 800, "h": 600},
            },
        }
        event = RoomMessageImage.from_dict(source)
        msg = matrix_client._build_event_message(event)
        self.assertTrue(msg["has_media"])
        self.assertEqual(msg["media_url"], "mxc://example.com/media123")
        self.assertEqual(msg["msgtype"], "m.image")
        self.assertEqual(msg["body"], "photo.jpg")
        self.assertEqual(msg["media_info"]["mimetype"], "image/jpeg")

    def test_text_message(self):
        source = {
            "type": "m.room.message",
            "event_id": "$txt1",
            "sender": "@bob:example.com",
            "origin_server_ts": 2000,
            "content": {"msgtype": "m.text", "body": "Hello world"},
        }
        event = RoomMessageText.from_dict(source)
        msg = matrix_client._build_event_message(event)
        self.assertEqual(msg["body"], "Hello world")
        self.assertFalse(msg.get("has_media", False))
        self.assertEqual(msg["msgtype"], "m.text")

    def test_reaction_event(self):
        source = {
            "type": "m.reaction",
            "event_id": "$react1",
            "sender": "@alice:example.com",
            "origin_server_ts": 3000,
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$target1",
                    "key": "\U0001f44d",
                }
            },
        }
        event = ReactionEvent.from_dict(source)
        msg = matrix_client._build_event_message(event)
        self.assertEqual(msg["type"], "m.reaction")
        self.assertEqual(msg["key"], "\U0001f44d")
        self.assertEqual(msg["relates_to"], "$target1")

    def test_sticker_event(self):
        source = {
            "type": "m.sticker",
            "event_id": "$sticker1",
            "sender": "@alice:example.com",
            "origin_server_ts": 4000,
            "content": {
                "body": "cute sticker",
                "url": "mxc://example.com/sticker1",
                "info": {"mimetype": "image/png", "size": 5000},
            },
        }
        event = StickerEvent.from_dict(source)
        msg = matrix_client._build_event_message(event)
        self.assertEqual(msg["type"], "m.sticker")
        self.assertTrue(msg["has_media"])
        self.assertEqual(msg["media_url"], "mxc://example.com/sticker1")

    def test_redacted_event(self):
        source = {
            "type": "m.room.message",
            "event_id": "$redacted1",
            "sender": "@alice:example.com",
            "origin_server_ts": 5000,
            "unsigned": {
                "redacted_because": {
                    "sender": "@mod:example.com",
                    "content": {"reason": "spam"},
                    "event_id": "$redact_event",
                    "origin_server_ts": 5001,
                }
            },
            "content": {},
        }
        event = RedactedEvent.from_dict(source)
        msg = matrix_client._build_event_message(event)
        self.assertEqual(msg["type"], "m.room.redacted")

    def test_redaction_event(self):
        source = {
            "type": "m.room.redaction",
            "event_id": "$redaction1",
            "sender": "@mod:example.com",
            "origin_server_ts": 6000,
            "redacts": "$target_event",
            "content": {"reason": "inappropriate"},
        }
        event = RedactionEvent.from_dict(source)
        msg = matrix_client._build_event_message(event)
        self.assertEqual(msg["type"], "m.room.redaction")
        self.assertEqual(msg["redacts"], "$target_event")

    def test_room_name_event(self):
        source = {
            "type": "m.room.name",
            "event_id": "$name1",
            "sender": "@admin:example.com",
            "origin_server_ts": 7000,
            "state_key": "",
            "content": {"name": "New Room Name"},
        }
        event = RoomNameEvent.from_dict(source)
        msg = matrix_client._build_event_message(event)
        self.assertEqual(msg["type"], "m.room.name")
        self.assertEqual(msg["name"], "New Room Name")

    def test_room_topic_event(self):
        source = {
            "type": "m.room.topic",
            "event_id": "$topic1",
            "sender": "@admin:example.com",
            "origin_server_ts": 8000,
            "state_key": "",
            "content": {"topic": "Discuss project updates"},
        }
        event = RoomTopicEvent.from_dict(source)
        msg = matrix_client._build_event_message(event)
        self.assertEqual(msg["type"], "m.room.topic")
        self.assertEqual(msg["topic"], "Discuss project updates")

    def test_call_hangup_event(self):
        source = {
            "type": "m.call.hangup",
            "event_id": "$call1",
            "sender": "@alice:example.com",
            "origin_server_ts": 9000,
            "content": {"call_id": "call_abc", "version": 0},
        }
        event = CallHangupEvent.from_dict(source)
        msg = matrix_client._build_event_message(event)
        self.assertEqual(msg["call_id"], "call_abc")
        self.assertEqual(msg["type"], "m.call.hangup")


class CreateRoomPowerLevelsTest(TestCase):
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._get_client_params")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._make_client")
    def test_room_created_with_invite_power_level_100(
        self, mock_make_client, mock_get_params
    ):
        from nio import RoomCreateResponse

        mock_client = mock.AsyncMock()
        mock_client.room_create.return_value = RoomCreateResponse(
            room_id="!new:example.com"
        )
        mock_make_client.return_value = mock_client
        mock_get_params.return_value = (
            "http://matrix.example.com",
            "@bot:example.com",
            "token",
        )

        room_id, alias_was_set = matrix_client.create_room("Test Room")

        self.assertEqual(room_id, "!new:example.com")
        self.assertFalse(alias_was_set)
        mock_client.room_create.assert_called_once()
        call_kwargs = mock_client.room_create.call_args[1]
        # Power level 100 gates the privileged actions (invite/kick/ban/redact
        # and every state event); m.room.message stays at 0 so members can
        # post freely. The org.matrix.msc3401.call.member event is lowered to
        # 0 to let users join LiveKit calls.
        self.assertEqual(
            call_kwargs["power_level_override"],
            {
                "invite": 100,
                "kick": 100,
                "ban": 100,
                "redact": 100,
                "events_default": 0,
                "state_default": 100,
                "events": {
                    "m.room.message": 0,
                    "m.room.name": 100,
                    "m.room.topic": 100,
                    "m.room.avatar": 100,
                    "m.room.power_levels": 100,
                    "m.room.join_rules": 100,
                    "m.room.history_visibility": 100,
                    "m.room.canonical_alias": 100,
                    "org.matrix.msc3401.call.member": 0,
                },
            },
        )


@mock.patch("waldur_mastermind.matrix_chat.matrix_client._get_client_params")
@mock.patch("waldur_mastermind.matrix_chat.matrix_client._make_client")
class SetPowerLevelTest(TestCase):
    ROOM_ID = "!room:example.com"
    USER_ID = "@alice:example.com"

    def _set_power_level(
        self, mock_make_client, mock_get_params, users, level, **room_defaults
    ):
        client = mock.AsyncMock()
        client.room_get_state_event.return_value = RoomGetStateEventResponse(
            content={"users": users, "state_default": 100, **room_defaults},
            event_type="m.room.power_levels",
            state_key="",
            room_id=self.ROOM_ID,
        )
        client.room_put_state.return_value = RoomPutStateResponse(
            event_id="$event", room_id=self.ROOM_ID
        )
        mock_make_client.return_value = client
        mock_get_params.return_value = (
            "http://matrix.example.com",
            "@bot:example.com",
            "token",
        )
        matrix_client.set_power_level(self.ROOM_ID, self.USER_ID, level)
        return client

    def test_raises_a_level(self, mock_make_client, mock_get_params):
        client = self._set_power_level(
            mock_make_client, mock_get_params, {"@bot:example.com": 100}, 50
        )

        client.room_put_state.assert_called_once_with(
            self.ROOM_ID,
            "m.room.power_levels",
            {
                "users": {"@bot:example.com": 100, self.USER_ID: 50},
                "state_default": 100,
            },
        )

    def test_lowering_to_the_default_takes_the_user_off_the_list(
        self, mock_make_client, mock_get_params
    ):
        client = self._set_power_level(
            mock_make_client,
            mock_get_params,
            {"@bot:example.com": 100, self.USER_ID: 50},
            0,
        )

        client.room_put_state.assert_called_once_with(
            self.ROOM_ID,
            "m.room.power_levels",
            {"users": {"@bot:example.com": 100}, "state_default": 100},
        )

    def test_leaves_an_unchanged_level_alone(self, mock_make_client, mock_get_params):
        client = self._set_power_level(
            mock_make_client, mock_get_params, {self.USER_ID: 50}, 50
        )

        client.room_put_state.assert_not_called()

    def test_leaves_an_unlisted_user_at_the_default_alone(
        self, mock_make_client, mock_get_params
    ):
        client = self._set_power_level(
            mock_make_client, mock_get_params, {"@bot:example.com": 100}, 0
        )

        client.room_put_state.assert_not_called()

    def test_writes_a_level_below_another_users_default(
        self, mock_make_client, mock_get_params
    ):
        client = self._set_power_level(
            mock_make_client, mock_get_params, {}, 0, users_default=10
        )

        client.room_put_state.assert_called_once_with(
            self.ROOM_ID,
            "m.room.power_levels",
            {"users": {self.USER_ID: 0}, "state_default": 100, "users_default": 10},
        )


class DownloadMediaTest(TestCase):
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._run_async")
    def test_download_media_success(self, mock_run_async):
        mock_run_async.return_value = (b"file-content", "image/jpeg", "photo.jpg")
        content_bytes, content_type, filename = matrix_client.download_media(
            "mxc://example.com/abc"
        )
        self.assertEqual(content_bytes, b"file-content")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(filename, "photo.jpg")

    @mock.patch("waldur_mastermind.matrix_chat.matrix_client._run_async")
    def test_download_media_error(self, mock_run_async):
        mock_run_async.side_effect = matrix_client.MatrixClientError("Download failed")
        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.download_media("mxc://example.com/bad")


@override_config(
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
class RoomMembershipAsUserTest(TestCase):
    """Joins and leaves go through the appservice so they mint no device or token."""

    room_id = "!room:matrix.example.com"
    user_id = "@alice:matrix.example.com"

    @respx.mock
    def test_join_uses_appservice_token_for_user(self):
        route = respx.post(
            f"https://matrix.example.com/_matrix/client/v3/join/{self.room_id}"
        ).mock(return_value=httpx.Response(200, json={"room_id": self.room_id}))

        self.assertTrue(matrix_client.join_room_as_user(self.room_id, self.user_id))

        request = route.calls.last.request
        self.assertEqual(request.headers["Authorization"], "Bearer test-as-token")
        self.assertEqual(request.url.params["user_id"], self.user_id)

    @respx.mock
    def test_join_without_invite_raises(self):
        respx.post(
            f"https://matrix.example.com/_matrix/client/v3/join/{self.room_id}"
        ).mock(
            return_value=httpx.Response(
                403,
                json={
                    "errcode": "M_FORBIDDEN",
                    "error": "cannot join a room that is not `public`",
                },
            )
        )

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.join_room_as_user(self.room_id, self.user_id)

    @respx.mock
    def test_leave_uses_appservice_token_for_user(self):
        route = respx.post(
            f"https://matrix.example.com/_matrix/client/v3/rooms/{self.room_id}/leave"
        ).mock(return_value=httpx.Response(200, json={}))

        self.assertTrue(matrix_client.leave_room_as_user(self.room_id, self.user_id))

        request = route.calls.last.request
        self.assertEqual(request.headers["Authorization"], "Bearer test-as-token")
        self.assertEqual(request.url.params["user_id"], self.user_id)

    @respx.mock
    def test_leave_when_not_in_room_succeeds(self):
        respx.post(
            f"https://matrix.example.com/_matrix/client/v3/rooms/{self.room_id}/leave"
        ).mock(
            return_value=httpx.Response(
                403, json={"errcode": "M_FORBIDDEN", "error": "User is not in the room"}
            )
        )

        self.assertTrue(matrix_client.leave_room_as_user(self.room_id, self.user_id))

    @respx.mock
    def test_join_through_a_failing_proxy_raises(self):
        respx.post(
            f"https://matrix.example.com/_matrix/client/v3/join/{self.room_id}"
        ).mock(
            return_value=httpx.Response(
                502,
                text="<html>Bad Gateway</html>",
                headers={"content-type": "text/html"},
            )
        )

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.join_room_as_user(self.room_id, self.user_id)

    @respx.mock
    def test_garbled_json_error_bodies_raise_client_errors(self):
        # A proxy error page labelled as JSON, or a body that is not an object.
        for body in [b"", b"<html>Bad Gateway</html>", b"null", b"\xe9\xe9"]:
            for call, path in [
                (matrix_client.join_room_as_user, f"join/{self.room_id}"),
                (matrix_client.leave_room_as_user, f"rooms/{self.room_id}/leave"),
            ]:
                with self.subTest(body=body, call=call.__name__):
                    respx.post(
                        f"https://matrix.example.com/_matrix/client/v3/{path}"
                    ).mock(
                        return_value=httpx.Response(
                            502,
                            content=body,
                            headers={"content-type": "application/json"},
                        )
                    )

                    with self.assertRaises(matrix_client.MatrixClientError):
                        call(self.room_id, self.user_id)

    @mock.patch.object(matrix_client, "_run_async")
    def test_bot_is_not_kicked(self, mock_run_async):
        # As with leaving: a stored member ID mapped onto the bot must not
        # remove the bot from a room it administers.
        self.assertFalse(
            matrix_client.kick_user(self.room_id, matrix_client.get_bot_user_id())
        )

        mock_run_async.assert_not_called()

    @mock.patch.object(matrix_client, "_run_async")
    def test_bot_is_not_made_to_leave(self, mock_run_async):
        # A stored ID that maps onto the bot must not take the bot out of a room
        # it administers.
        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.leave_room_as_user(
                self.room_id, matrix_client.get_bot_user_id()
            )

        mock_run_async.assert_not_called()


@override_config(
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
    MATRIX_USER_REGISTRATION_SECRET="test-secret",
    MATRIX_USER_ID_FORMAT="username",
)
class BotIdentityIsReservedTest(TestCase):
    @mock.patch.object(matrix_client, "_run_async")
    def test_user_whose_id_would_be_the_bot_is_not_provisioned(self, mock_run_async):
        # The appservice acts for any ID Waldur maps a user to, so a user mapped
        # onto the bot would act as the bot in every room.
        user = structure_factories.UserFactory(username="Waldur-Bot")

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.ensure_user_exists(user)

        mock_run_async.assert_not_called()
        self.assertFalse(
            matrix_client.MatrixUserProfile.objects.filter(user=user).exists()
        )

    @respx.mock
    def test_homeserver_errors_fail_registration_instead_of_falling_through(self):
        # A proxy error page would otherwise read as "flow not offered" and fall
        # through to appservice registration, which sets no password on Tuwunel.
        user = structure_factories.UserFactory()
        for body, content_type in [
            (b"<html>Bad Gateway</html>", "text/html"),
            (b"<html>Bad Gateway</html>", "application/json"),
        ]:
            with self.subTest(content_type=content_type):
                route = respx.post(
                    "https://matrix.example.com/_matrix/client/v3/register"
                ).mock(
                    return_value=httpx.Response(
                        502, content=body, headers={"content-type": content_type}
                    )
                )
                calls_before = route.call_count

                with self.assertRaises(matrix_client.MatrixClientError):
                    matrix_client.ensure_user_exists(user)

                self.assertEqual(route.call_count - calls_before, 1)
                self.assertFalse(
                    matrix_client.MatrixUserProfile.objects.filter(
                        user=user, provisioned=True
                    ).exists()
                )

    @mock.patch.object(matrix_client, "_run_async")
    def test_provisioned_profile_mapped_to_the_bot_is_refused(self, mock_run_async):
        # Rows like this predate the reservation, or appear when the bot's
        # localpart is changed to one a user already holds.
        user = structure_factories.UserFactory()
        matrix_client.MatrixUserProfile.objects.create(
            user=user, matrix_user_id=matrix_client.get_bot_user_id(), provisioned=True
        )

        with self.assertRaises(matrix_client.MatrixClientError) as cm:
            matrix_client.ensure_user_exists(user)

        self.assertIn("profile", str(cm.exception))
        mock_run_async.assert_not_called()


@override_config(
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
    SITE_NAME="Waldur",
)
class WebSessionTest(TestCase):
    user_id = "@alice:matrix.example.com"
    login_url = "https://matrix.example.com/_matrix/client/v3/login"

    def _login_response(self, request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "user_id": self.user_id,
                "device_id": body["device_id"],
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in_ms": 300000,
            },
        )

    @respx.mock
    def test_logs_in_through_appservice_on_a_new_web_device(self):
        route = respx.post(self.login_url).mock(side_effect=self._login_response)

        session = matrix_client.create_web_session(self.user_id)

        request = route.calls.last.request
        body = json.loads(request.content)
        self.assertEqual(request.headers["Authorization"], "Bearer test-as-token")
        self.assertEqual(body["type"], "m.login.application_service")
        self.assertEqual(
            body["identifier"], {"type": "m.id.user", "user": self.user_id}
        )
        self.assertTrue(body["refresh_token"])
        self.assertTrue(body["device_id"].startswith(matrix_client.WEB_DEVICE_PREFIX))
        self.assertEqual(
            session,
            {
                "device_id": body["device_id"],
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in_ms": 300000,
            },
        )

    @respx.mock
    def test_every_session_gets_its_own_device(self):
        # Tuwunel keeps one refresh token per device, so two sessions on one
        # device would revoke each other.
        respx.post(self.login_url).mock(side_effect=self._login_response)

        first = matrix_client.create_web_session(self.user_id)
        second = matrix_client.create_web_session(self.user_id)

        self.assertNotEqual(first["device_id"], second["device_id"])

    @respx.mock
    def test_homeserver_without_refresh_tokens_returns_no_expiry(self):
        respx.post(self.login_url).mock(
            return_value=httpx.Response(
                200, json={"device_id": "WALDUR_WEB_X", "access_token": "access-1"}
            )
        )

        session = matrix_client.create_web_session(self.user_id)

        self.assertIsNone(session["refresh_token"])
        self.assertIsNone(session["expires_in_ms"])

    @respx.mock
    def test_unreachable_homeserver_raises_client_error(self):
        respx.post(self.login_url).mock(side_effect=httpx.ConnectError("refused"))

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.create_web_session(self.user_id)

    @respx.mock
    def test_login_without_access_token_raises_client_error(self):
        respx.post(self.login_url).mock(
            return_value=httpx.Response(200, json={"device_id": "WALDUR_WEB_X"})
        )

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.create_web_session(self.user_id)

    @respx.mock
    def test_rejected_login_raises(self):
        respx.post(self.login_url).mock(
            return_value=httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
        )

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.create_web_session(self.user_id)

    @respx.mock
    def test_bot_gets_no_session(self):
        route = respx.post(self.login_url).mock(side_effect=self._login_response)

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.create_web_session(matrix_client.get_bot_user_id())

        self.assertFalse(route.called)


@override_config(
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
class DeviceManagementTest(TestCase):
    user_id = "@alice:matrix.example.com"

    @respx.mock
    def test_lists_devices_through_appservice(self):
        route = respx.get("https://matrix.example.com/_matrix/client/v3/devices").mock(
            return_value=httpx.Response(
                200, json={"devices": [{"device_id": "A", "last_seen_ts": 1}]}
            )
        )

        devices = matrix_client.list_devices(self.user_id)

        self.assertEqual(devices, [{"device_id": "A", "last_seen_ts": 1}])
        request = route.calls.last.request
        self.assertEqual(request.headers["Authorization"], "Bearer test-as-token")
        self.assertEqual(request.url.params["user_id"], self.user_id)

    @respx.mock
    def test_logs_out_a_device_by_signing_in_on_it(self):
        # The appservice cannot DELETE a device without UIA, but a token on the
        # device can log it out, which removes the device and all its tokens.
        login = respx.post("https://matrix.example.com/_matrix/client/v3/login").mock(
            return_value=httpx.Response(
                200, json={"device_id": "WALDUR_WEB_A", "access_token": "kill"}
            )
        )
        logout = respx.post("https://matrix.example.com/_matrix/client/v3/logout").mock(
            return_value=httpx.Response(200, json={})
        )

        matrix_client.logout_device(self.user_id, "WALDUR_WEB_A")

        login_body = json.loads(login.calls.last.request.content)
        self.assertEqual(login_body["device_id"], "WALDUR_WEB_A")
        # Expires on the homeserver's access_token_ttl should /logout fail; it
        # replaces the device's own refresh token, which is going anyway.
        self.assertTrue(login_body["refresh_token"])
        # The device keeps its name: it may be someone's Element session.
        self.assertNotIn("initial_device_display_name", login_body)
        self.assertEqual(
            logout.calls.last.request.headers["Authorization"], "Bearer kill"
        )


@override_config(
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
class LogoutWebDevicesTest(TestCase):
    @mock.patch.object(matrix_client, "logout_device")
    @mock.patch.object(
        matrix_client,
        "list_devices",
        return_value=[
            {"device_id": "WALDUR_WEB_A"},
            {"device_id": "ELEMENT_PHONE"},
            {"device_id": "WALDUR_WEB_B"},
        ],
    )
    def test_signs_out_only_web_devices(self, mock_list, mock_logout):
        matrix_client.logout_web_devices("@alice:matrix.example.com")

        self.assertEqual(
            mock_logout.call_args_list,
            [
                mock.call("@alice:matrix.example.com", "WALDUR_WEB_A"),
                mock.call("@alice:matrix.example.com", "WALDUR_WEB_B"),
            ],
        )


class LogoutEveryWebDeviceTest(TestCase):
    @mock.patch.object(matrix_client, "logout_device")
    @mock.patch.object(
        matrix_client,
        "list_devices",
        return_value=[{"device_id": "WALDUR_WEB_A"}, {"device_id": "WALDUR_WEB_B"}],
    )
    def test_one_failure_does_not_stop_the_rest(self, mock_list, mock_logout):
        mock_logout.side_effect = [matrix_client.MatrixClientError("boom"), None]

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.logout_web_devices("@alice:matrix.example.com")

        self.assertEqual(mock_logout.call_count, 2)


class LogoutAllDevicesTest(TestCase):
    @mock.patch.object(matrix_client, "logout_device")
    @mock.patch.object(
        matrix_client,
        "list_devices",
        return_value=[
            {"device_id": "WALDUR_WEB_A"},
            {"device_id": "ELEMENT_PHONE"},
        ],
    )
    def test_signs_out_external_clients_too(self, mock_list, mock_logout):
        mock_logout.side_effect = [None, matrix_client.MatrixClientError("boom")]

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.logout_all_devices("@alice:matrix.example.com")

        self.assertEqual(
            mock_logout.call_args_list,
            [
                mock.call("@alice:matrix.example.com", "WALDUR_WEB_A"),
                mock.call("@alice:matrix.example.com", "ELEMENT_PHONE"),
            ],
        )


class StaleWebDevicesTest(TestCase):
    now_ms = 10 * 24 * 3600 * 1000
    hour_ms = 3600 * 1000

    def _device(self, device_id, hours_ago):
        return {
            "device_id": device_id,
            "last_seen_ts": self.now_ms - hours_ago * self.hour_ms,
        }

    def test_ignores_devices_waldur_did_not_create(self):
        devices = [self._device("ELEMENT", 100), self._device("WALDUR_WEB_A", 1)]

        self.assertEqual(matrix_client.stale_web_devices(devices, self.now_ms), [])

    def test_selects_web_devices_idle_past_the_refresh_window(self):
        devices = [
            self._device("WALDUR_WEB_OLD", 25),
            self._device("WALDUR_WEB_NEW", 1),
        ]

        self.assertEqual(
            matrix_client.stale_web_devices(devices, self.now_ms), ["WALDUR_WEB_OLD"]
        )

    def test_keeps_only_the_most_recently_seen_web_devices(self):
        devices = [
            self._device(f"WALDUR_WEB_{i:02d}", i)
            for i in range(matrix_client.MAX_WEB_DEVICES + 2)
        ]

        stale = matrix_client.stale_web_devices(devices, self.now_ms)

        self.assertEqual(
            sorted(stale),
            [
                f"WALDUR_WEB_{matrix_client.MAX_WEB_DEVICES:02d}",
                f"WALDUR_WEB_{matrix_client.MAX_WEB_DEVICES + 1:02d}",
            ],
        )

    def test_device_never_seen_is_not_idle(self):
        # Some homeservers fill last_seen_ts only on the first request, so a
        # brand-new device would otherwise look idle forever.
        devices = [{"device_id": "WALDUR_WEB_A", "last_seen_ts": None}]

        self.assertEqual(matrix_client.stale_web_devices(devices, self.now_ms), [])

    def test_device_being_kept_is_never_stale(self):
        devices = [self._device("WALDUR_WEB_NEW", 48)]

        self.assertEqual(
            matrix_client.stale_web_devices(
                devices, self.now_ms, keep_device_id="WALDUR_WEB_NEW"
            ),
            [],
        )

    def test_recently_seen_devices_are_never_evicted(self):
        # More live tabs than the cap would otherwise evict each other in turn.
        devices = [
            {"device_id": f"WALDUR_WEB_{i:02d}", "last_seen_ts": self.now_ms - i * 1000}
            for i in range(matrix_client.MAX_WEB_DEVICES + 2)
        ]

        self.assertEqual(matrix_client.stale_web_devices(devices, self.now_ms), [])


class PowerLevelForScopeTest(TestCase):
    # Whoever may create a project's room is an admin in it, as the project
    # admin is. Staff become moderators by joining a room, not through this.

    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.user = structure_factories.UserFactory()

    def _level(self):
        return matrix_client.get_power_level_for_scope(self.user, self.project)

    def test_customer_role_that_can_create_rooms_is_admin(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)
        self.project.customer.add_user(self.user, CustomerRole.OWNER)

        self.assertEqual(self._level(), 50)

    def test_owner_who_cannot_create_rooms_is_not_admin(self):
        self.project.customer.add_user(self.user, CustomerRole.OWNER)

        self.assertEqual(self._level(), 0)

    def test_project_role_that_can_create_rooms_is_admin(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)
        self.project.add_user(self.user, ProjectRole.MANAGER)

        self.assertEqual(self._level(), 50)

    def test_project_admin_is_admin(self):
        self.project.add_user(self.user, ProjectRole.ADMIN)

        self.assertEqual(self._level(), 50)

    def test_room_creation_on_another_customer_does_not_count(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)
        structure_factories.CustomerFactory().add_user(self.user, CustomerRole.OWNER)
        self.project.add_user(self.user, ProjectRole.MEMBER)

        self.assertEqual(self._level(), 0)

    def test_staff_project_member_is_not_admin(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.project.add_user(self.user, ProjectRole.MEMBER)

        self.assertEqual(self._level(), 0)
