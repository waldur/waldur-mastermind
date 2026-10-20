import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from nio import (
    RoomGetStateError,
    RoomGetStateResponse,
    RoomPutStateError,
    RoomPutStateResponse,
)
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.matrix_chat import (
    bot,
    bot_state,
    commands,
    matrix_client,
    models,
    tasks,
    tickets,
)
from waldur_mastermind.matrix_chat.tests import fixtures
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.backend import SupportBackendType

ENABLED = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="https://matrix.example.com",
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="as-token",
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.BASIC,
)
MEMBER = "@member:matrix.example.com"
THREAD = {"m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}
NAMES = [command.name for command in commands.COMMANDS]


class CommandListTest(SimpleTestCase):
    def test_every_command_has_a_handler(self):
        for command in commands.COMMANDS:
            top = command.name.split()[0]
            self.assertTrue(top in tasks.COMMAND_HANDLERS or top == "ticket", top)
        self.assertEqual(
            set(tasks.COMMAND_HANDLERS),
            {c.name for c in commands.COMMANDS if " " not in c.name} - {"ticket"},
        )

    def test_help_lists_every_command(self):
        text = tasks._cmd_help("!r", MEMBER, "$e")
        for command in commands.COMMANDS:
            self.assertIn(f"!{command.name}", text)

    def test_subcommands_are_found(self):
        self.assertEqual(commands.find("ticket", "send").name, "ticket send")
        self.assertEqual(commands.find("ticket", "VM is down").name, "ticket")
        self.assertEqual(commands.find("ticket", " SEND ").name, "ticket send")

    def test_a_sub_command_takes_no_more_words(self):
        # What a draft is about may start with a sub-command's name.
        self.assertEqual(commands.find("ticket", "send now").name, "ticket")
        self.assertTrue(tickets.wants_draft(tasks.parse_command("!ticket send now")))

    def test_a_structured_invocation_is_parsed(self):
        content = {
            "body": "whatever the client wrote",
            "m.mentions": {"user_ids": ["@bot:x"]},
            commands.INVOCATION_KEY: {
                "command": "ticket",
                "arguments": {"description": "VM  is\ndown"},
            },
        }
        self.assertEqual(
            commands.invocation_text(content, "@bot:x"), "ticket VM is down"
        )
        command = tasks.parse_command("whatever the client wrote", content)
        self.assertEqual((command.name, command.args), ("ticket", "VM is down"))
        content[commands.INVOCATION_KEY] = {"command": "ticket send"}
        self.assertEqual(tasks.parse_command("x", content).args, "send")

    def test_a_structured_invocation_must_mention_the_bot(self):
        content = {commands.INVOCATION_KEY: {"command": "status"}}
        self.assertIsNone(commands.invocation_text(content, "@bot:x"))
        self.assertEqual(commands.invocation_text(content), "status")
        # The bot parses with its own ID: the text wins without the mention.
        command = tasks.parse_command("!help", content, "@bot:x")
        self.assertEqual(command.name, "help")

    def test_an_unknown_structured_command_is_ignored(self):
        content = {
            "m.mentions": {"user_ids": ["@bot:x"]},
            commands.INVOCATION_KEY: {"command": "ban"},
        }
        self.assertIsNone(commands.invocation_text(content, "@bot:x"))

    def test_the_state_key_is_msc4391_s(self):
        expected = base64.b64encode(
            hashlib.sha256(b"ticket send@bot:example.org").digest()
        ).decode()
        self.assertEqual(
            commands.state_key("ticket send", "@bot:example.org"), expected
        )
        self.assertTrue(expected.endswith("="))

    def test_a_description_holds_nothing_but_the_command(self):
        content = commands.description_content(commands.BY_NAME["ticket"])
        self.assertEqual(content["command"], "ticket")
        self.assertEqual(
            content["parameters"],
            [
                {
                    "key": "description",
                    "schema": {"schema_type": "primitive", "type": "string"},
                    "description": {
                        "m.text": [{"body": "What is wrong, in a few words"}]
                    },
                    "optional": True,
                }
            ],
        )
        self.assertEqual(
            content["description"]["m.text"][0]["body"],
            commands.BY_NAME["ticket"].description,
        )


@override_config(**ENABLED)
class CommandTestBase(TestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room
        self.member = self.fixture.member
        models.MatrixUserProfile.objects.create(user=self.member, matrix_user_id=MEMBER)
        support_models.RequestType.objects.create(
            name="Incident", issue_type_name="Incident"
        )
        bot_state.acquire_lease(matrix_client.get_bot_user_id(), "holder")

    def _draft(self):
        return models.MatrixTicketDraft.objects.create(
            room=self.room,
            requester=self.member,
            requester_matrix_id=MEMBER,
            source_event_id="$root",
            thread_root="$root",
            content=json.dumps(
                {
                    "title": "t",
                    "description": "d",
                    "type": "Incident",
                    "priority": "",
                    "resource_name": "",
                    "resource_uuid": "",
                    "generated_by": "plain",
                }
            ),
            expires_at=timezone.now() + timedelta(minutes=10),
        )

    def _listed(self):
        return {c["name"]: c for c in commands.for_user(self.room, self.member)}


class AvailabilityTest(CommandTestBase):
    def _reasons(self):
        return {name: c["reason"] for name, c in self._listed().items()}

    def test_a_member_can_use_the_commands(self):
        listed = self._listed()
        for name in ("help", "status", "orders", "members", "ticket"):
            self.assertTrue(listed[name]["available"], name)
            self.assertEqual(listed[name]["reason"], "")
        self.assertEqual(listed["ticket"]["hint"], "[description]")

    def test_send_and_cancel_need_an_open_draft(self):
        listed = self._listed()
        self.assertEqual(listed["ticket send"]["reason"], commands.NO_OPEN_DRAFT)
        self.assertEqual(listed["ticket cancel"]["reason"], commands.NO_OPEN_DRAFT)
        self._draft()
        listed = self._listed()
        self.assertTrue(listed["ticket send"]["available"])
        self.assertTrue(listed["ticket cancel"]["available"])

    def test_an_expired_draft_is_no_draft(self):
        self._draft()
        models.MatrixTicketDraft.objects.update(expires_at=timezone.now())
        self.assertFalse(self._listed()["ticket send"]["available"])

    @override_config(MATRIX_ENABLED=False)
    def test_nothing_without_matrix(self):
        self.assertEqual(set(self._reasons().values()), {commands.NOT_ENABLED})

    def test_nothing_while_the_bot_is_down(self):
        models.MatrixBotIdentity.objects.update(lease_expires_at=timezone.now())
        self.assertEqual(set(self._reasons().values()), {commands.BOT_NOT_RUNNING})

    def test_nothing_in_an_inactive_room(self):
        self.room.state = models.RoomStates.DISABLING
        self.room.save()
        self.assertEqual(set(self._reasons().values()), {commands.ROOM_INACTIVE})

    def test_nothing_in_a_room_without_project(self):
        self.room.scope = self.fixture.customer
        self.room.save()
        self.assertEqual(set(self._reasons().values()), {commands.NOT_A_PROJECT_ROOM})

    def test_nothing_without_a_role(self):
        self.fixture.project.remove_user(self.member)
        self.assertEqual(set(self._reasons().values()), {commands.NO_ACCESS})

    def test_staff_without_a_role_have_none(self):
        staff = self.fixture.staff
        listed = commands.for_user(self.room, staff)
        self.assertEqual({c["reason"] for c in listed}, {commands.NO_ACCESS})

    @override_config(WALDUR_SUPPORT_ENABLED=False)
    def test_tickets_need_the_helpdesk(self):
        reasons = self._reasons()
        self.assertEqual(reasons["status"], "")
        for name in ("ticket", "ticket send", "ticket cancel"):
            self.assertEqual(reasons[name], commands.HELPDESK_DISABLED)

    def test_tickets_need_a_request_type(self):
        support_models.RequestType.objects.update(is_active=False)
        self.assertEqual(self._reasons()["ticket"], commands.HELPDESK_DISABLED)

    def test_tickets_need_a_role_that_may_report(self):
        # Customer support who also run the organization's chat sit in its
        # rooms, but the support API does not let them report for a project.
        CustomerRole.SUPPORT.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)
        support = self.fixture.customer_support
        support.email = "support@example.com"
        support.save()
        reasons = {
            c["name"]: c["reason"] for c in commands.for_user(self.room, support)
        }
        self.assertEqual(reasons["status"], "")
        self.assertEqual(reasons["ticket"], commands.CANNOT_REPORT)

    def test_tickets_need_email_and_name(self):
        self.member.email = ""
        self.member.save()
        reasons = self._reasons()
        self.assertEqual(reasons["help"], "")
        self.assertEqual(reasons["ticket"], commands.PROFILE_INCOMPLETE)


class ParityTest(CommandTestBase):
    """A command listed as available is accepted by its handler, and one
    listed as unavailable is refused with the listed reason."""

    SCENARIOS = {
        "ok": lambda self: None,
        "draft": lambda self: self._draft(),
        "no role": lambda self: self.fixture.project.remove_user(self.member),
        "helpdesk off": lambda self: support_models.RequestType.objects.update(
            is_active=False
        ),
        "no reporter role": lambda self: (
            self.fixture.project.remove_user(self.member),
            CustomerRole.SUPPORT.add_permission(PermissionEnum.CREATE_MATRIX_ROOM),
            self.fixture.customer.add_user(self.member, CustomerRole.SUPPORT),
        ),
        "no email": lambda self: type(self.member)
        .objects.filter(pk=self.member.pk)
        .update(email=""),
        "customer room": lambda self: models.MatrixRoom.objects.filter(
            pk=self.room.pk
        ).update(
            content_type=ContentType.objects.get_for_model(self.fixture.customer),
            object_id=self.fixture.customer.pk,
        ),
    }

    def _handle(self, name):
        """The handler's refusal for ``!name``, or None when it accepted."""
        if name == "ticket":
            request = tickets.start(
                self.room.room_id, MEMBER, "$cmd", tasks.parse_command("!ticket")
            )
            if request is not None:
                return None
        else:
            tasks.answer_command(self.room.room_id, MEMBER, "$cmd", f"!{name}", THREAD)
        reply = (
            models.MatrixOutboxMessage.objects.filter(reply_to="$cmd")
            .exclude(body="")
            .values_list("body", flat=True)
            .first()
        )
        refusals = {
            commands.ROOM_INACTIVE,
            commands.NOT_A_PROJECT_ROOM,
            commands.NO_ACCESS,
            commands.HELPDESK_DISABLED,
            commands.PROFILE_INCOMPLETE,
            commands.CANNOT_REPORT,
            commands.NO_DRAFT,
        }
        return reply if reply in refusals else None

    @mock.patch("openai.OpenAI")
    def test_the_list_and_the_handlers_agree(self, openai):
        for scenario, arrange in self.SCENARIOS.items():
            for name in NAMES:
                with self.subTest(scenario=scenario, command=name):
                    with transaction.atomic():
                        arrange(self)
                        self.member.refresh_from_db()
                        self.room.refresh_from_db()
                        listed = self._listed()[name]
                        refusal = self._handle(name)
                        if listed["available"]:
                            self.assertIsNone(refusal)
                        else:
                            expected = (
                                commands.NO_DRAFT
                                if listed["reason"] == commands.NO_OPEN_DRAFT
                                else listed["reason"]
                            )
                            self.assertEqual(refusal, expected)
                        transaction.set_rollback(True)


@override_config(**ENABLED)
class CommandsApiTest(test.APITestCase):
    def setUp(self):
        CommandTestBase.setUp(self)
        self.url = reverse("matrix-room-commands", kwargs={"uuid": self.room.uuid.hex})

    def test_lists_the_caller_s_commands(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_name = {c["name"]: c for c in response.data}
        self.assertEqual(list(by_name), NAMES)
        self.assertTrue(by_name["ticket"]["available"])
        self.assertEqual(by_name["ticket send"]["reason"], commands.NO_OPEN_DRAFT)

    def test_the_room_names_its_bot(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(
            reverse("matrix-room-detail", kwargs={"uuid": self.room.uuid.hex})
        )
        self.assertEqual(response.data["bot_user_id"], "@waldur-bot:matrix.example.com")

    def test_a_stranger_does_not_see_the_room(self):
        self.client.force_authenticate(self.fixture.user)
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND
        )


@override_config(**ENABLED)
class DescriptionsTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room
        support_models.RequestType.objects.create(
            name="Incident", issue_type_name="Incident"
        )
        self.bot_user_id = matrix_client.get_bot_user_id()

    def _described(self):
        wanted = commands.rooms_to_describe()[self.room.room_id]
        return {
            name: wanted[commands.state_key(name, self.bot_user_id)] for name in NAMES
        }

    def test_a_project_room_describes_every_command(self):
        described = self._described()
        for name in NAMES:
            self.assertEqual(described[name]["command"], name)

    @override_config(WALDUR_SUPPORT_ENABLED=False)
    def test_without_helpdesk_ticket_commands_are_withdrawn(self):
        described = self._described()
        self.assertEqual(described["status"]["command"], "status")
        for name in ("ticket", "ticket send", "ticket cancel"):
            self.assertEqual(described[name], {})

    def test_a_room_without_project_describes_none(self):
        self.room.scope = self.fixture.customer
        self.room.save()
        self.assertEqual(set(map(json.dumps, self._described().values())), {"{}"})

    def test_inactive_rooms_are_left_alone(self):
        self.room.state = models.RoomStates.DISABLING
        self.room.save()
        self.assertEqual(commands.rooms_to_describe(), {})


ROOM_ID = "!room:matrix.example.com"
BOT = "@waldur-bot:matrix.example.com"


class PublishTest(SimpleTestCase):
    def _bot(self, held):
        instance = bot.MatrixBot(
            "holder",
            bot.BotSettings(
                homeserver_url="https://matrix.example.com",
                as_token="t",
                localpart="waldur-bot",
                user_id=BOT,
                display_name="Waldur Bot",
            ),
        )
        instance.client = mock.Mock()
        instance.client.rooms = {ROOM_ID: mock.Mock()}
        instance.client.room_get_state = mock.AsyncMock(
            return_value=RoomGetStateResponse(
                [
                    {
                        "type": commands.COMMAND_DESCRIPTION_EVENT,
                        "state_key": key,
                        "content": content,
                    }
                    for key, content in held.items()
                ],
                ROOM_ID,
            )
        )
        instance.client.room_put_state = mock.AsyncMock(
            return_value=RoomPutStateResponse("$e", ROOM_ID)
        )
        return instance

    def _wanted(self, helpdesk_on=True):
        return commands.room_descriptions(BOT, True, helpdesk_on)

    def test_writes_only_what_differs(self):
        wanted = self._wanted()
        held = dict(wanted)
        key = commands.state_key("ticket", BOT)
        del held[key]
        instance = self._bot(held)
        self.assertTrue(async_to_sync(instance._describe_commands)(ROOM_ID, wanted))
        instance.client.room_put_state.assert_awaited_once_with(
            ROOM_ID, commands.COMMAND_DESCRIPTION_EVENT, wanted[key], state_key=key
        )

    def test_nothing_is_written_when_the_room_is_current(self):
        wanted = self._wanted()
        instance = self._bot(wanted)
        async_to_sync(instance._describe_commands)(ROOM_ID, wanted)
        instance.client.room_put_state.assert_not_awaited()

    def test_a_withdrawn_command_is_emptied(self):
        instance = self._bot(self._wanted())
        wanted = self._wanted(helpdesk_on=False)
        async_to_sync(instance._describe_commands)(ROOM_ID, wanted)
        self.assertEqual(instance.client.room_put_state.await_count, 3)
        for call in instance.client.room_put_state.await_args_list:
            self.assertEqual(call.args[2], {})

    def test_a_refusal_is_tried_again_later(self):
        instance = self._bot({})
        instance.client.room_put_state.return_value = RoomPutStateError("forbidden")
        self.assertFalse(
            async_to_sync(instance._describe_commands)(ROOM_ID, self._wanted())
        )

    def test_a_room_joined_during_a_check_is_described_at_once(self):
        instance = self._bot({})
        instance._renew_lease = mock.AsyncMock()
        attempts = []

        async def run():
            instance._synced.set()
            described = asyncio.Event()

            async def describe(room_id, descriptions):
                attempts.append(room_id)
                if len(attempts) == 1:
                    # The bot joins the room while this check is still going.
                    instance._describe_now.set()
                    return False
                described.set()
                return True

            instance._describe_commands = describe
            loop_task = asyncio.create_task(instance.describe_commands_forever())
            try:
                await asyncio.wait_for(described.wait(), 2)
            finally:
                loop_task.cancel()
                await asyncio.gather(loop_task, return_exceptions=True)

        with (
            mock.patch.object(
                commands, "rooms_to_describe", return_value={ROOM_ID: self._wanted()}
            ),
            mock.patch.object(bot, "COMMANDS_CHECK_SECONDS", 3600),
        ):
            async_to_sync(run)()
        self.assertEqual(attempts, [ROOM_ID, ROOM_ID])

    def test_a_room_not_joined_yet_waits(self):
        instance = self._bot({})
        instance.client.room_get_state.return_value = RoomGetStateError(
            "not in room", "M_FORBIDDEN"
        )
        self.assertFalse(
            async_to_sync(instance._describe_commands)(ROOM_ID, self._wanted())
        )
        instance.client.room_put_state.assert_not_awaited()
