from unittest import mock

from django.test import TestCase
from nio import (
    CallHangupEvent,
    ReactionEvent,
    RedactedEvent,
    RedactionEvent,
    RoomMessageImage,
    RoomMessageText,
    RoomNameEvent,
    RoomTopicEvent,
    StickerEvent,
)

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
