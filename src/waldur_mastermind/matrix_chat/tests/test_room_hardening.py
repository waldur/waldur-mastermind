from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from nio import RoomCreateError, RoomCreateResponse

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.matrix_chat import (
    formatting,
    handlers,
    matrix_client,
    models,
    tasks,
)

EVIL_NAME = "Win a prize [here](https://evil.example) ![x](https://evil.example/p.png)"
# A line break ends the paragraph, so the next line can open a block of its own.
MULTILINE_NAME = "vm\n> [Verify account](https://evil.example)"


def _room_for(project):
    return models.MatrixRoom.objects.create(
        room_id="!r:hs",
        room_name="room",
        state=models.RoomStates.ACTIVE,
        content_type=ContentType.objects.get_for_model(project),
        object_id=project.id,
    )


@mock.patch("waldur_mastermind.matrix_chat.matrix_client._get_client_params")
@mock.patch("waldur_mastermind.matrix_chat.matrix_client._make_client")
class UnfederatedRoomsTest(TestCase):
    def _client(self, mock_make_client, mock_get_params, *responses):
        client = mock.AsyncMock()
        client.room_create.side_effect = responses
        mock_make_client.return_value = client
        mock_get_params.return_value = ("http://hs", "@bot:hs", "token")
        return client

    def test_rooms_are_created_without_federation(
        self, mock_make_client, mock_get_params
    ):
        # Project rooms are for one Waldur's users; m.federate=false keeps every
        # other server out for good, whatever the homeserver's settings.
        client = self._client(
            mock_make_client, mock_get_params, RoomCreateResponse(room_id="!r:hs")
        )

        matrix_client.create_room("Project", alias_localpart="waldur-1")

        self.assertIs(client.room_create.call_args[1]["federate"], False)

    def test_the_retry_without_an_alias_is_unfederated_too(
        self, mock_make_client, mock_get_params
    ):
        client = self._client(
            mock_make_client,
            mock_get_params,
            RoomCreateError(message="M_ROOM_IN_USE: Room alias already taken"),
            RoomCreateResponse(room_id="!r:hs"),
        )

        matrix_client.create_room("Project", alias_localpart="waldur-1")

        self.assertEqual(client.room_create.call_count, 2)
        self.assertIs(client.room_create.call_args_list[1][1]["federate"], False)


class MarkdownSafeTextTest(TestCase):
    def test_escaped_text_renders_without_links_or_images(self):
        html = matrix_client.render_markdown(formatting.escape_markdown(EVIL_NAME))

        self.assertNotIn("<a", html)
        self.assertNotIn("<img", html)
        self.assertIn("[here](https://evil.example)", html)

    def test_a_code_span_cannot_be_closed_from_inside(self):
        html = matrix_client.render_markdown(
            "Resource " + formatting.code_span("x` [here](https://evil.example) `")
        )

        self.assertNotIn("<a", html)

    def test_a_code_span_cannot_be_left_open_by_its_last_character(self):
        # Waldur's chat takes a backslash before the closing backtick as an
        # escaped backtick, and an empty span as the opening of a longer one,
        # so the span would run on into the next name on the line.
        self.assertEqual(formatting.code_span("vm\\"), "` vm\\ `")
        self.assertEqual(formatting.code_span(""), "`  `")
        self.assertEqual(formatting.code_span("vm"), "`vm`")

    def test_a_code_span_changes_nothing_but_backticks(self):
        # Inside a code span an escape would show as typed.
        self.assertEqual(formatting.code_span("my_vm* [x]"), "`my_vm* [x]`")
        self.assertEqual(formatting.code_span("a`b"), "`a'b`")

    def test_a_padded_code_span_shows_the_name_alone(self):
        html = matrix_client.render_markdown(
            f"Resources {formatting.code_span('vm' + chr(92))}, "
            f"{formatting.code_span('[here](https://evil.example)')}"
        )

        self.assertIn("<code>vm\\</code>", html)
        self.assertNotIn("<a", html)

    def test_line_breaks_cannot_open_a_block(self):
        for helper in (formatting.escape_markdown, formatting.code_span):
            with self.subTest(helper=helper.__name__):
                html = matrix_client.render_markdown(
                    f"Resource {helper(MULTILINE_NAME)} is ready."
                )

                self.assertNotIn("<a", html)
                self.assertNotIn("<blockquote", html)

    def test_every_kind_of_whitespace_becomes_one_space(self):
        # Waldur's chat deletes a form feed before it parses, which would join
        # the halves of "~~" or "://" after they had been checked apart, and a
        # carriage return starts a new line as a line feed does.
        self.assertEqual(
            formatting.escape_markdown("~\x0c~x http:\x0c//y a\rb"),
            "~ ~x http: //y a b",
        )

    def test_harmless_punctuation_stays_unescaped(self):
        # Every escape shows as a backslash in the plain-text body that push
        # notifications, previews and history exports display.
        name = "2.0 Team (prod) #1 | ~infra! > staging"

        self.assertEqual(formatting.escape_markdown(name), name)

    def test_a_name_leading_a_message_cannot_open_a_block(self):
        # Some bot messages start with a user's name.
        for name in (
            "# Notice",
            "> quote",
            "- item",
            "+ item",
            "1. item",
            "2) item",
            "10. item",
            "1.",
            " # Notice",
            " 1. item",
            "~~~Mallory",
        ):
            with self.subTest(name=name):
                html = matrix_client.render_markdown(
                    f"{formatting.escape_markdown(name)} joined the room."
                )

                self.assertTrue(html.startswith("<p>"), html)
                self.assertIn(name.strip(), html.replace("&gt;", ">"))
                self.assertIn("joined the room.", html)

    def test_every_special_character_gets_its_own_backslash(self):
        # Waldur's chat renders the plain-text body with another Markdown
        # library, where a link survives unless the opening bracket itself is
        # escaped, so the exact text matters and not only what it renders to.
        self.assertEqual(
            formatting.escape_markdown("\\ ` * _ [x] <"),
            "\\\\ \\` \\* \\_ \\[x\\] \\<",
        )

    def test_a_url_in_a_name_is_not_a_link_that_swallows_escapes(self):
        # In Waldur's chat a bare URL runs up to the next "<" and takes the
        # backslash before it along, which would leave "<!--" free to hide
        # the bot's words up to a "-->" in the requester's name.
        offering = formatting.escape_markdown("https://x.example/<!--")
        requester = formatting.escape_markdown("--> Bob")

        self.assertEqual(offering, "https\\://x.example/\\<!--")
        self.assertEqual(
            matrix_client.render_markdown(
                f"Order approved: Create of {offering} (requested by {requester})."
            ),
            "<p>Order approved: Create of https://x.example/&lt;!-- "
            "(requested by --&gt; Bob).</p>",
        )

    def test_paired_tildes_and_equals_are_escaped(self):
        # Waldur's chat renders the plain-text body, where ~~ strikes through
        # and == highlights up to the next pair, which may sit in another name.
        self.assertEqual(
            formatting.escape_markdown("~~Admin~~ ==Owner== ~~~"),
            r"\~\~Admin\~\~ \=\=Owner\=\= \~\~\~",
        )
        self.assertEqual(formatting.escape_markdown("a = b ~c 12:30"), "a = b ~c 12:30")

    def test_escaped_pairs_render_literally(self):
        html = matrix_client.render_markdown(
            f"{formatting.escape_markdown('~~Admin~~ ==Owner==')} joined the room."
        )

        self.assertEqual(html, "<p>~~Admin~~ ==Owner== joined the room.</p>")

    def test_each_escaped_character_keeps_its_markup_out(self):
        # One name per character the escape covers: an autolink, a link whose
        # brackets arrive already escaped, emphasis and a code span.
        for name, tag in (
            ("<https://evil.example>", "<a"),
            (r"\[here\](https://evil.example)", "<a"),
            ("**URGENT**", "<strong"),
            ("_urgent_", "<em"),
            ("`urgent`", "<code"),
        ):
            with self.subTest(name=name):
                html = matrix_client.render_markdown(
                    f"{formatting.escape_markdown(name)} joined the room."
                )

                self.assertNotIn(tag, html)


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class AnnouncementsEscapeNamesTest(TestCase):
    def test_role_announcement_shows_a_users_name_literally(
        self, mock_client, mock_tasks
    ):
        # Users choose their own names, and the bot posts with power level 100,
        # so a name must not turn into a link in its message.
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        models.MatrixRoom.objects.create(
            room_id="!r:hs",
            room_name=project.name,
            state=models.RoomStates.ACTIVE,
            content_type=ContentType.objects.get_for_model(project),
            object_id=project.id,
        )
        user = structure_factories.UserFactory(first_name=EVIL_NAME, last_name="")

        with self.captureOnCommitCallbacks(execute=True):
            project.add_user(user, ProjectRole.MEMBER)

        body = mock_tasks.send_room_notification.delay.call_args[0][1]
        self.assertNotIn("<a", matrix_client.render_markdown(body))

    def test_role_announcement_keeps_a_multiline_name_on_one_line(
        self, mock_client, mock_tasks
    ):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        models.MatrixRoom.objects.create(
            room_id="!r:hs",
            room_name=project.name,
            state=models.RoomStates.ACTIVE,
            content_type=ContentType.objects.get_for_model(project),
            object_id=project.id,
        )
        user = structure_factories.UserFactory(first_name=MULTILINE_NAME, last_name="")

        with self.captureOnCommitCallbacks(execute=True):
            project.add_user(user, ProjectRole.MEMBER)

        body = mock_tasks.send_room_notification.delay.call_args[0][1]
        html = matrix_client.render_markdown(body)
        self.assertNotIn("<a", html)
        self.assertNotIn("<blockquote", html)

    def test_role_revocation_shows_a_users_name_literally(
        self, mock_client, mock_tasks
    ):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        _room_for(project)
        user = structure_factories.UserFactory(first_name=EVIL_NAME, last_name="")
        project.add_user(user, ProjectRole.MEMBER)

        with self.captureOnCommitCallbacks(execute=True):
            project.remove_user(user)

        body = mock_tasks.send_room_notification.delay.call_args[0][1]
        self.assertIn("has lost", body)
        self.assertNotIn("<a", matrix_client.render_markdown(body))

    def test_order_announcement_shows_offering_and_requester_names_literally(
        self, mock_client, mock_tasks
    ):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        _room_for(project)
        order = marketplace_factories.OrderFactory(
            project=project,
            offering=marketplace_factories.OfferingFactory(name=EVIL_NAME),
            created_by=structure_factories.UserFactory(
                first_name=EVIL_NAME, last_name=""
            ),
        )
        order.state = OrderStates.EXECUTING

        with self.captureOnCommitCallbacks(execute=True):
            handlers.on_order_state_changed(
                sender=type(order), instance=order, created=False
            )

        body = mock_tasks.send_room_notification.delay.call_args[0][1]
        self.assertIn("Order approved", body)
        self.assertNotIn("<a", matrix_client.render_markdown(body))


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class StaffAnnouncementsEscapeNamesTest(TestCase):
    def setUp(self):
        self.room = _room_for(structure_factories.ProjectFactory())
        self.staff = structure_factories.UserFactory(
            first_name=EVIL_NAME, last_name="", is_staff=True
        )

    def test_join_announcement_shows_the_name_literally(self, mock_client):
        mock_client.is_enabled.return_value = True
        mock_client.ensure_user_exists.return_value = "@staff:hs"

        tasks.staff_join_room(str(self.room.uuid), str(self.staff.uuid))

        body = models.MatrixOutboxMessage.objects.get(room=self.room).body
        self.assertIn("joined the room", body)
        self.assertNotIn("<a", matrix_client.render_markdown(body))

    def test_leave_announcement_shows_the_name_literally(self, mock_client):
        mock_client.is_enabled.return_value = True
        models.MatrixRoomMember.objects.create(
            room=self.room,
            user=self.staff,
            matrix_user_id="@staff:hs",
            power_level=50,
            membership_state=models.MembershipStates.JOINED,
        )

        tasks.staff_leave_room(str(self.room.uuid), str(self.staff.uuid))

        body = models.MatrixOutboxMessage.objects.get(room=self.room).body
        self.assertIn("left the room", body)
        self.assertNotIn("<a", matrix_client.render_markdown(body))


class CommandRepliesEscapeNamesTest(TestCase):
    def test_orders_reply_shows_resource_and_project_names_literally(self):
        project = structure_factories.ProjectFactory(name=EVIL_NAME)
        resource = marketplace_factories.ResourceFactory(
            project=project, name="vm` [here](https://evil.example) `"
        )
        marketplace_factories.OrderFactory(project=project, resource=resource)
        models.MatrixRoom.objects.create(
            room_id="!r:hs",
            room_name="room",
            state=models.RoomStates.ACTIVE,
            content_type=ContentType.objects.get_for_model(project),
            object_id=project.id,
        )

        reply = tasks._cmd_orders("!r:hs", "@alice:hs", "$e")

        self.assertNotIn("<a", matrix_client.render_markdown(reply))

    def test_orders_reply_keeps_multiline_names_on_one_line(self):
        project = structure_factories.ProjectFactory(name=MULTILINE_NAME)
        resource = marketplace_factories.ResourceFactory(
            project=project, name=MULTILINE_NAME
        )
        marketplace_factories.OrderFactory(project=project, resource=resource)
        models.MatrixRoom.objects.create(
            room_id="!r:hs",
            room_name="room",
            state=models.RoomStates.ACTIVE,
            content_type=ContentType.objects.get_for_model(project),
            object_id=project.id,
        )

        reply = tasks._cmd_orders("!r:hs", "@alice:hs", "$e")

        html = matrix_client.render_markdown(reply)
        self.assertNotIn("<a", html)
        self.assertNotIn("<blockquote", html)

    def test_status_reply_shows_project_and_resource_names_literally(self):
        project = structure_factories.ProjectFactory(name=EVIL_NAME)
        marketplace_factories.ResourceFactory(
            project=project,
            name="vm` [here](https://evil.example) `",
            state=ResourceStates.ERRED,
        )
        _room_for(project)

        reply = tasks._cmd_status("!r:hs", "@alice:hs", "$e")

        self.assertIn("errored", reply)
        self.assertNotIn("<a", matrix_client.render_markdown(reply))

    def test_replies_for_an_empty_project_show_its_name_literally(self):
        _room_for(structure_factories.ProjectFactory(name=EVIL_NAME))

        for command in (tasks._cmd_status, tasks._cmd_orders):
            with self.subTest(command=command.__name__):
                reply = command("!r:hs", "@alice:hs", "$e")

                self.assertNotIn("<a", matrix_client.render_markdown(reply))

    @mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
    def test_unknown_command_echo_cannot_turn_into_a_link(self, mock_client):
        mock_client.is_enabled.return_value = True

        # A command is one word: the dispatcher passes on the first token.
        reply = tasks._command_reply(
            "!r:hs", "@alice:hs", "$e", "x`[here](https://evil.example)`"
        )

        self.assertNotIn("<a", matrix_client.render_markdown(reply))
