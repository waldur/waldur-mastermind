"""The bot's commands: what each is, and who may use it where.

One list describes every command, and one set of checks decides whether a
command works for a user in a room. The bot's handlers refuse with these checks,
the API lists each user's commands with them, and the bot advertises the
commands that can work in a room with them, so the three never disagree.

The advertisement is a room state event per command, in the format of MSC4391
("Simplified in-room bot commands", unstable prefix): clients offer the
commands in their composer. State events are not encrypted; a command's name
and description are nothing private.
"""

import base64
import dataclasses
import hashlib
import types

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from waldur_core.configuration import config
from waldur_core.structure.models import Project
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import serializers as support_serializers

from . import bot_state, matrix_client, models

COMMAND_DESCRIPTION_EVENT = "org.matrix.msc4391.command_description"
# The structured invocation a client may send in a message, besides the text.
INVOCATION_KEY = "org.matrix.msc4391.command"

NOT_ENABLED = "Matrix chat is not enabled."
BOT_NOT_RUNNING = "The chat bot is not running."
ROOM_INACTIVE = "This room is not active."
NOT_A_PROJECT_ROOM = "This room is not linked to a project."
NO_ACCESS = "You don't have access to this room's project in Waldur."
HELPDESK_DISABLED = (
    "Support tickets cannot be drafted here: the helpdesk is not enabled in Waldur."
)
PROFILE_INCOMPLETE = (
    "Your Waldur profile needs an email address and a full name before you can "
    "open support tickets."
)
CANNOT_REPORT = (
    "Only the organization's owners and the project's managers, administrators "
    "and members can open support tickets for this project."
)
NO_DRAFT = (
    "There is no ticket draft of yours in this thread. Send `!ticket` to make one."
)
NO_OPEN_DRAFT = "You have no open ticket draft in this room."


# Checks. Each returns the reason a command cannot be used, or None.


def room_reason(room):
    if room is None or room.state != models.RoomStates.ACTIVE:
        return ROOM_INACTIVE
    if room.project is None:
        return NOT_A_PROJECT_ROOM
    return None


def member_reason(user, room):
    if user is None or not user.is_active or not models.has_room_role(user, room):
        return NO_ACCESS
    return None


def helpdesk_enabled():
    return bool(config.WALDUR_SUPPORT_ENABLED) and (
        support_models.RequestType.objects.filter(is_active=True).exists()
    )


def helpdesk_reason():
    return None if helpdesk_enabled() else HELPDESK_DISABLED


def profile_reason(user):
    return None if user.email and user.full_name else PROFILE_INCOMPLETE


def reporter_reason(user, project):
    """The support API's own check of who may report an issue for a project,
    so nobody is offered a draft the API would refuse."""
    serializer = support_serializers.IssueSerializer(
        context={"request": types.SimpleNamespace(user=user)}
    )
    try:
        serializer.validate_project(project)
    except ValidationError:
        return CANNOT_REPORT
    return None


def open_draft_reason(user, room):
    has_draft = models.MatrixTicketDraft.objects.filter(
        room=room,
        requester=user,
        state=models.TicketDraftStates.PENDING,
        expires_at__gt=timezone.now(),
    ).exists()
    return None if has_draft else NO_OPEN_DRAFT


@dataclasses.dataclass(frozen=True)
class Parameter:
    key: str
    description: str
    optional: bool = True
    type: str = "string"


@dataclasses.dataclass(frozen=True)
class BotCommand:
    """A command: ``name`` as typed after ``!`` (``"ticket send"``).

    ``helpdesk`` commands need the helpdesk, which decides for the whole room;
    ``profile`` ones need the user's email and name, and a role the support
    API lets report issues for the project; ``draft`` ones an open draft of
    the user's in the room.
    """

    name: str
    description: str
    parameters: tuple = ()
    helpdesk: bool = False
    profile: bool = False
    draft: bool = False

    @property
    def hint(self):
        return " ".join(
            f"[{p.key}]" if p.optional else f"<{p.key}>" for p in self.parameters
        )

    def reason(self, room, user):
        """Why ``user`` cannot use the command in ``room``, or None. The order
        is the handlers': where, who, then what the command needs."""
        return (
            room_reason(room)
            or member_reason(user, room)
            or (self.helpdesk and helpdesk_reason())
            or (
                self.profile
                and (profile_reason(user) or reporter_reason(user, room.project))
            )
            or (self.draft and open_draft_reason(user, room))
            or None
        )


COMMANDS = (
    BotCommand("help", "Show the available commands"),
    BotCommand("status", "Show the project's resource status summary"),
    BotCommand("orders", "Show the project's last 5 orders"),
    BotCommand("members", "List the room's members and their roles"),
    BotCommand(
        "ticket",
        "Draft a support ticket from this conversation (as a reply or in a thread)",
        parameters=(Parameter("description", "What is wrong, in a few words"),),
        helpdesk=True,
        profile=True,
    ),
    BotCommand(
        "ticket send",
        "Send your ticket draft to the helpdesk (reply to it if you have several)",
        helpdesk=True,
        profile=True,
        draft=True,
    ),
    BotCommand(
        "ticket cancel",
        "Drop your ticket draft (reply to it if you have several)",
        helpdesk=True,
        profile=True,
        draft=True,
    ),
)

BY_NAME = {command.name: command for command in COMMANDS}


def find(name, command_args=""):
    """The command a parsed ``!name args`` is: ``ticket send`` when the
    arguments are just ``send``, rather than ``ticket`` with an argument.
    Sub-commands take no arguments, and what a draft is about may well start
    with one's name (``!ticket send button fails``)."""
    return BY_NAME.get(
        f"{name} {' '.join(command_args.split()).lower()}"
    ) or BY_NAME.get(name)


def invocation_text(content, bot_user_id=None):
    """A structured MSC4391 invocation in a message's ``content`` as the text
    the bot parses (``"ticket VM is down"``), or None.

    Only commands the bot has, with string arguments it knows; with
    ``bot_user_id``, only one that mentions the bot, as MSC4391 asks clients
    to, so it is not taken for another bot's command.
    """
    block = content.get(INVOCATION_KEY) if isinstance(content, dict) else None
    if not isinstance(block, dict):
        return None
    command = BY_NAME.get(block.get("command"))
    if command is None:
        return None
    if bot_user_id is not None:
        mentions = content.get("m.mentions")
        user_ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
        if not isinstance(user_ids, list) or bot_user_id not in user_ids:
            return None
    arguments = block.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    values = [
        " ".join(arguments[p.key].split())
        for p in command.parameters
        if isinstance(arguments.get(p.key), str)
    ]
    return " ".join([command.name, *values]).strip()


def sender_user(sender):
    """The active Waldur user behind a Matrix sender, or None."""
    profile = (
        models.MatrixUserProfile.objects.select_related("user")
        .filter(matrix_user_id=sender)
        .first()
    )
    if profile is None or not profile.user.is_active:
        return None
    return profile.user


def for_user(room, user):
    """Every command, and whether ``user`` can use it in ``room`` now."""
    if not matrix_client.is_enabled():
        common = NOT_ENABLED
    elif not bot_state.is_bot_running(matrix_client.get_bot_user_id()):
        common = BOT_NOT_RUNNING
    else:
        common = None
    listed = []
    for command in COMMANDS:
        reason = common or command.reason(room, user)
        listed.append(
            {
                "name": command.name,
                "description": command.description,
                "hint": command.hint,
                "available": reason is None,
                "reason": reason or "",
            }
        )
    return listed


# The advertisement.


def state_key(command_name, bot_user_id):
    """MSC4391: the padded base64 SHA-256 of the command and the bot's ID."""
    digest = hashlib.sha256((command_name + bot_user_id).encode()).digest()
    return base64.b64encode(digest).decode()


def description_content(command):
    def text(body):
        return {"m.text": [{"body": body}]}

    return {
        "command": command.name,
        "description": text(command.description),
        "parameters": [
            {
                "key": parameter.key,
                "schema": {"schema_type": "primitive", "type": parameter.type},
                "description": text(parameter.description),
                **({"optional": True} if parameter.optional else {}),
            }
            for parameter in command.parameters
        ],
    }


def room_descriptions(bot_user_id, project_room, helpdesk_on):
    """``{state key: content}`` a room's command state should hold: a
    description of each command that can work there, and empty content, which
    clients ignore, for the others (a state event cannot be removed)."""
    return {
        state_key(command.name, bot_user_id): (
            description_content(command)
            if project_room and (helpdesk_on or not command.helpdesk)
            else {}
        )
        for command in COMMANDS
    }


def rooms_to_describe():
    """Active rooms, by Matrix room ID, with what their command state should
    hold. Cheap enough to ask every minute: two queries, whatever the number
    of rooms."""
    bot_user_id = matrix_client.get_bot_user_id()
    helpdesk_on = helpdesk_enabled()
    project_type = ContentType.objects.get_for_model(Project)
    rooms = (
        models.MatrixRoom.objects.filter(state=models.RoomStates.ACTIVE)
        .exclude(room_id=None)
        .values_list("room_id", "content_type_id")
    )
    return {
        room_id: room_descriptions(
            bot_user_id, content_type_id == project_type.id, helpdesk_on
        )
        for room_id, content_type_id in rooms
    }
