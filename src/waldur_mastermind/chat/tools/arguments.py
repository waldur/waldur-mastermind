"""Checks tool arguments against the tool's inputSchema before the tool runs.

Tools trust their arguments to match the schema, so ToolExecutor runs this for
every caller: chat, the REST execute endpoint and MCP.
"""

import re

from jsonschema import Draft202012Validator, ValidationError
from jsonschema.exceptions import best_match

from waldur_mastermind.chat.tools.base import ToolDefinition

MAX_ARGUMENT_DEPTH = 32
MAX_ECHO_CHARS = 64
MAX_LISTED_NAMES = 5
# PostgreSQL refuses NUL, and UTF-8 can't encode a lone surrogate.
UNSTORABLE_TEXT = re.compile("[\x00\ud800-\udfff]")


def shorten(text: str) -> str:
    return text if len(text) <= MAX_ECHO_CHARS else f"{text[:MAX_ECHO_CHARS]}..."


def schema_error(schema: dict, value) -> ValidationError | None:
    # No format checker: it wants dashes in UUIDs, and Waldur's have none.
    return best_match(Draft202012Validator(schema).iter_errors(value))


def without_nulls(value, depth: int = 0):
    """``value`` without null object properties, at any depth.

    Clients send null for options left unset, and tools read it as missing.
    Nulls in lists stay, for the schema to judge.
    """
    # Deeper input is refused by structure_problem; stopping here keeps the
    # recursion bounded.
    if depth > MAX_ARGUMENT_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: without_nulls(item, depth + 1)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [without_nulls(item, depth + 1) for item in value]
    return value


def structure_problem(arguments: dict, max_chars: int | None = None) -> str | None:
    """What a schema can't say about these arguments.

    A loop, because this much nesting is what breaks recursion.
    """
    remaining = max_chars
    stack = [(arguments, 0)]
    while stack:
        value, depth = stack.pop()
        if remaining is not None:
            remaining -= len(value) if isinstance(value, str) else 1
            if remaining < 0:
                return f"over {max_chars} characters in total."
        if depth > MAX_ARGUMENT_DEPTH:
            return "nested too deeply."
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for pair in value.items() for item in pair)
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str) and UNSTORABLE_TEXT.search(value):
            return "text can't contain NUL or unpaired surrogate characters."
    return None


def unknown_arguments_error(arguments: dict, schema: dict) -> str | None:
    """Names the arguments the schema doesn't declare.

    Schemas allow extra keys, but a tool that drops a misnamed filter would
    present an unfiltered list as filtered.
    """
    allowed = list(schema.get("properties", {}))
    unknown = sorted(set(arguments) - set(allowed))
    if not unknown:
        return None
    names = ", ".join(shorten(name) for name in unknown[:MAX_LISTED_NAMES])
    if len(unknown) > MAX_LISTED_NAMES:
        names += f" and {len(unknown) - MAX_LISTED_NAMES} more"
    return (
        f"Unknown argument: {names}. Valid arguments: {', '.join(allowed) or 'none'}."
    )


def argument_error(
    definition: ToolDefinition, arguments, max_chars: int | None = None
) -> str | None:
    """Why these arguments can't be passed to the tool, in terms a model can fix."""
    if not isinstance(arguments, dict):
        return "Invalid arguments: expected an object."
    # First, because the schema check's messages repeat the value they refuse.
    if problem := structure_problem(arguments, max_chars):
        return f"Invalid arguments: {problem}"
    if error := unknown_arguments_error(arguments, definition.inputSchema):
        return error
    error = schema_error(definition.inputSchema, arguments)
    if error:
        message = error.message
        # Unknown-property messages name the object's keys, not the object.
        keys = list(error.instance) if isinstance(error.instance, dict) else []
        for value in map(repr, [error.instance, *keys]):
            message = message.replace(value, shorten(value))
        return f"Invalid arguments at {shorten(error.json_path)}: {message}"
    return None
