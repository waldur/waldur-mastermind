"""Serves the chat tool registry over MCP, as the caller.

Everything here touches the database or thread-locals, so the server runs it on
the ORM pool through run_orm_async.
"""

import json
import logging
import re
from contextlib import contextmanager

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from jsonschema import Draft202012Validator, ValidationError
from jsonschema.exceptions import best_match
from mcp import types
from mcp.server.auth.provider import AccessToken

from waldur_core.core import middleware as core_middleware
from waldur_core.core import utils as core_utils
from waldur_core.core.authentication import set_user_context
from waldur_core.core.models import PersonalAccessToken
from waldur_core.logging import event_logger
from waldur_core.logging import middleware as logging_middleware
from waldur_core.logging.enums import EventType
from waldur_mastermind.chat.tools.base import ToolDefinition
from waldur_mastermind.chat.tools.executor import ToolExecutor
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import get_tool_set_for_user
from waldur_mastermind.mcp.utils import MCP_PATH

logger = logging.getLogger(__name__)

MAX_ARGUMENT_DEPTH = 32
# The input guards' email pattern is quadratic and holds the GIL, so a long
# argument stalls every client: 0.06 s at this size, 7 s at 50,000 characters.
MAX_ARGUMENT_CHARS = 4000
MAX_ECHO_CHARS = 64
# PostgreSQL refuses NUL, and UTF-8 can't encode a lone surrogate.
UNSTORABLE_TEXT = re.compile("[\x00\ud800-\udfff]")


def load_pat(token: AccessToken) -> PersonalAccessToken:
    """The caller's token, unless it stopped being valid after it was verified.

    A call can wait for a pool thread, so this repeats the verifier's checks.
    """
    return PersonalAccessToken.objects.select_related("user").get(
        uuid=token.claims["pat_uuid"],
        is_active=True,
        expires_at__gt=timezone.now(),
        user__is_active=True,
        user__can_use_personal_access_tokens=True,
    )


def get_visible_definitions(pat: PersonalAccessToken) -> dict[str, ToolDefinition]:
    """The tools this caller may see and call, by name."""
    # A token is never the user's own sign-in, so it gets no HPC tools.
    permitted = set(get_tool_set_for_user(pat.user, delegated=True))
    return {
        name.value: definition
        for name, definition in tool_registry.definitions.items()
        if definition.mcp_exposed
        and name in permitted
        and definition.pat_allows(pat.scopes)
    }


def to_mcp_tool(definition: ToolDefinition) -> types.Tool:
    return types.Tool(
        name=definition.name.value,
        description=definition.description,
        input_schema=definition.inputSchema,
        output_schema=definition.output_schema,
        annotations=types.ToolAnnotations(read_only_hint=definition.read_only),
    )


def shorten(text: str) -> str:
    return text if len(text) <= MAX_ECHO_CHARS else f"{text[:MAX_ECHO_CHARS]}..."


def text_content(text: str) -> types.TextContent:
    return types.TextContent(type="text", text=text)


def error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[text_content(message)], is_error=True)


def schema_error(schema: dict, value) -> ValidationError | None:
    # No format checker: it wants dashes in UUIDs, and Waldur's have none.
    return best_match(Draft202012Validator(schema).iter_errors(value))


def argument_problem(arguments: dict) -> str | None:
    """What a schema can't say about these arguments.

    A loop, because this much nesting is what breaks recursion.
    """
    remaining = MAX_ARGUMENT_CHARS
    stack = [(arguments, 0)]
    while stack:
        value, depth = stack.pop()
        remaining -= len(value) if isinstance(value, str) else 1
        if remaining < 0:
            return f"over {MAX_ARGUMENT_CHARS} characters in total."
        if depth > MAX_ARGUMENT_DEPTH:
            return "nested too deeply."
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for pair in value.items() for item in pair)
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str) and UNSTORABLE_TEXT.search(value):
            return "text can't contain NUL or unpaired surrogate characters."
    return None


def argument_error(definition: ToolDefinition, arguments: dict) -> str | None:
    """Why these arguments can't be passed to the tool, in terms a model can fix.

    The SDK's low-level server doesn't check them, and tools assume they match
    the schema.
    """
    # First, because the schema check's messages repeat the value they refuse.
    if problem := argument_problem(arguments):
        return f"Invalid arguments: {problem}"
    error = schema_error(definition.inputSchema, arguments)
    if error:
        message = error.message
        # Unknown-property messages name the object's keys, not the object.
        keys = list(error.instance) if isinstance(error.instance, dict) else []
        for value in map(repr, [error.instance, *keys]):
            message = message.replace(value, shorten(value))
        return f"Invalid arguments at {shorten(error.json_path)}: {message}"
    return None


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
def caller_context(pat: PersonalAccessToken, client_ip: str | None):
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
        set_user_context(pat.user, skip_token_check=True)
        logging_middleware.set_current_auth(pat)
        yield
    finally:
        logging_middleware.reset_event_context()
        core_middleware.set_current_user(None)


def emit_tool_called(pat: PersonalAccessToken, tool_name: str) -> None:
    # Arguments stay out: they can hold personal data, and events are kept.
    event_logger.emit(
        "User {user_username} called MCP tool {tool_name} with {client}.",
        event_type=EventType.MCP_TOOL_CALLED,
        event_context={
            "user": pat.user,
            "client": pat.name,
            "tool_name": tool_name,
            "server": MCP_PATH,
        },
        scopes=[pat.user],
    )


def list_tools(token: AccessToken) -> list[types.Tool]:
    return [
        to_mcp_tool(definition)
        for definition in get_visible_definitions(load_pat(token)).values()
    ]


def call_tool(
    token: AccessToken, name: str, arguments: dict, client_ip: str | None
) -> types.CallToolResult:
    pat = load_pat(token)
    # The same answer as for a name that doesn't exist, so that callers can't
    # learn about tools outside their set.
    definition = get_visible_definitions(pat).get(name)
    if definition is None:
        return error_result(f"Unknown tool: {shorten(name)!r}")
    # Clients send null for options left unset, and tools read it as missing.
    arguments = {key: value for key, value in arguments.items() if value is not None}
    if error := argument_error(definition, arguments):
        return error_result(error)
    with caller_context(pat, client_ip):
        # Before the tool, so that nothing is read without a record of it.
        emit_tool_called(pat, name)
        result = ToolExecutor(pat.user, delegated=True).execute_tool(name, arguments)
        return to_call_result(result, definition)
