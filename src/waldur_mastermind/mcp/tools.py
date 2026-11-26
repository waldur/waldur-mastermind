"""Serves the chat tool registry over MCP, as the caller.

Everything here touches the database or thread-locals, so the server runs it on
the ORM pool through run_orm_async.
"""

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass

from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError, transaction
from django.utils import timezone
from mcp import types
from mcp.server.auth.provider import AccessToken

from waldur_core.core import middleware as core_middleware
from waldur_core.core import utils as core_utils
from waldur_core.core.authentication import set_user_context
from waldur_core.core.models import PersonalAccessToken, User
from waldur_core.logging import event_logger
from waldur_core.logging import middleware as logging_middleware
from waldur_core.logging.enums import EventType
from waldur_mastermind.chat.tools.arguments import (
    argument_error,
    schema_error,
    shorten,
    without_nulls,
)
from waldur_mastermind.chat.tools.base import ToolDefinition
from waldur_mastermind.chat.tools.executor import ToolExecutor
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import get_tool_set_for_user
from waldur_mastermind.mcp import auth, oauth
from waldur_mastermind.mcp.utils import MCP_PATH

logger = logging.getLogger(__name__)

TOOL_ERROR = "An error occurred while executing the tool."
# The input guards' email pattern is quadratic and holds the GIL, so a long
# argument stalls every client: 0.06 s at this size, 7 s at 50,000 characters.
MAX_ARGUMENT_CHARS = 4000


class InvalidToken(Exception):
    """The caller's token stopped being valid after it was verified."""


@dataclass(frozen=True)
class Caller:
    user: User
    # Named in the audit event: the PAT's name, or the OAuth client's.
    client: str
    pat: PersonalAccessToken | None = None
    oauth_client_id: str | None = None

    def allows(self, definition: ToolDefinition) -> bool:
        if self.pat:
            return definition.pat_allows(self.pat.scopes)
        # OAuth consent grants only read access, so OAuth callers get only
        # read-only tools.
        return definition.read_only


def load_caller(token: AccessToken) -> Caller:
    """Who the token acts for, unless it stopped being valid after it was verified.

    A call can wait for a pool thread, so this repeats the verifier's checks.
    """
    claims = token.claims or {}
    if "pat_uuid" in claims:
        try:
            pat = PersonalAccessToken.objects.select_related("user").get(
                uuid=claims["pat_uuid"],
                is_active=True,
                expires_at__gt=timezone.now(),
                user__is_active=True,
                user__can_use_personal_access_tokens=True,
            )
        except PersonalAccessToken.DoesNotExist:
            raise InvalidToken from None
        return Caller(user=pat.user, client=pat.name, pat=pat)

    oauth_token = auth.load_oauth_token(token.token)
    if oauth_token is None or oauth.missing_elevated_scope(
        oauth_token.user, oauth_token.scope.split()
    ):
        raise InvalidToken
    return Caller(
        user=oauth_token.user,
        client=oauth.get_client_name(oauth_token.application),
        oauth_client_id=oauth_token.application.client_id,
    )


def get_visible_definitions(caller: Caller) -> dict[str, ToolDefinition]:
    """The tools this caller may see and call, by name."""
    # A token is never the user's own sign-in, so it gets no HPC tools.
    permitted = set(get_tool_set_for_user(caller.user, delegated=True))
    return {
        name.value: definition
        for name, definition in tool_registry.definitions.items()
        if definition.mcp_exposed and name in permitted and caller.allows(definition)
    }


def to_mcp_tool(definition: ToolDefinition) -> types.Tool:
    return types.Tool(
        name=definition.name.value,
        description=definition.description,
        input_schema=definition.inputSchema,
        output_schema=definition.output_schema,
        annotations=types.ToolAnnotations(read_only_hint=definition.read_only),
    )


def text_content(text: str) -> types.TextContent:
    return types.TextContent(type="text", text=text)


def error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[text_content(message)], is_error=True)


def to_call_result(result: dict, definition: ToolDefinition) -> types.CallToolResult:
    if result["type"] != "success":
        return error_result(result["summary"])
    # Python's NaN and Infinity aren't JSON, so clients couldn't read them.
    encoded = json.dumps(result["data"], cls=DjangoJSONEncoder, allow_nan=False)
    data = json.loads(encoded)
    # Clients reject such a result without saying why, so it's caught here.
    if error := schema_error(definition.output_schema, data):
        # Where, not what: the value can be personal data.
        logger.error(
            "MCP tool %s broke its output schema at %s (%s).",
            definition.name.value,
            error.json_path,
            error.validator,
        )
        return error_result("The tool returned data in an unexpected format.")
    return types.CallToolResult(
        # Summaries say what was found, not what it is, and not every client
        # reads structuredContent. The spec asks for the JSON as text too.
        content=[text_content(result["summary"]), text_content(encoded)],
        structured_content=data,
    )


@contextmanager
def caller_context(caller: Caller, client_ip: str | None):
    """Attribute everything done in this block to the caller.

    Both contexts are thread-locals and the pool's threads serve every user, so
    they must not outlive the call.
    """
    client_ip = core_utils.normalize_ip_address(client_ip)
    try:
        # Replaced, not merged: set_current_user adds to what it finds.
        logging_middleware.set_event_context(
            {"ip_address": client_ip} if client_ip else {}
        )
        set_user_context(caller.user, skip_token_check=True)
        if caller.pat:
            logging_middleware.set_current_auth(caller.pat)
        yield
    finally:
        logging_middleware.reset_event_context()
        core_middleware.set_current_user(None)


def emit_tool_called(caller: Caller, tool_name: str) -> None:
    event_context = {
        "user": caller.user,
        "client": caller.client,
        "tool_name": tool_name,
        "server": MCP_PATH,
    }
    if caller.oauth_client_id:
        event_context["oauth_client_id"] = caller.oauth_client_id
    # Arguments stay out: they can hold personal data, and events are kept.
    event_logger.emit(
        "User {user_username} called MCP tool {tool_name} with {client}.",
        event_type=EventType.MCP_TOOL_CALLED,
        event_context=event_context,
        scopes=[caller.user],
    )


def list_tools(token: AccessToken) -> list[types.Tool]:
    return [
        to_mcp_tool(definition)
        for definition in get_visible_definitions(load_caller(token)).values()
    ]


def call_tool(
    token: AccessToken, name: str, arguments: dict, client_ip: str | None
) -> types.CallToolResult:
    caller = load_caller(token)
    # The same answer as for a name that doesn't exist, so that callers can't
    # learn about tools outside their set.
    definition = get_visible_definitions(caller).get(name)
    if definition is None:
        return error_result(f"Unknown tool: {shorten(name)!r}")
    arguments = without_nulls(arguments)
    # Here as well as in the executor, so that refused arguments leave no audit
    # record, and so that the size limit applies.
    if error := argument_error(definition, arguments, MAX_ARGUMENT_CHARS):
        return error_result(error)
    with caller_context(caller, client_ip):
        # Before the tool, so that nothing is read without a record of it.
        emit_tool_called(caller, name)
        # A savepoint, so a statement timeout undoes the tool but not the audit
        # record. The executor logs and swallows the query's error, so Django
        # finds the savepoint unreleasable, rolls it back and raises.
        try:
            with transaction.atomic():
                result = ToolExecutor(caller.user, delegated=True).execute_tool(
                    name, arguments
                )
        except DatabaseError:
            logger.warning("MCP tool %s was rolled back.", name)
            return error_result(TOOL_ERROR)
        return to_call_result(result, definition)
