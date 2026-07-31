"""Sentry event processing for structlog-rendered log records.

structlog is configured with ``ProcessorFormatter.wrap_for_formatter`` (see
``waldur_core.server.base_settings``), so ``LogRecord.msg`` holds the structlog
event *dict* and rendering is deferred to each handler's ``ProcessorFormatter``.
sentry-sdk's logging integration reads the record message directly, so it sees
``str(event_dict)`` - the message, the log level, the logger name and any
rendered traceback fused into one opaque string.

Sentry groups log-derived events by that string, so a single underlying bug
mints a fresh issue group for every distinct identifier or traceback it
contains. ``before_send`` restores the readable message, moves the remaining
structlog keys into the event's extra data, and pins a grouping fingerprint that
ignores volatile identifiers.

This module is imported from the deployment settings file, so it must not import
Django models or anything that needs the app registry.
"""

import ast
import re

# Keys that may carry the human-readable message, in order of preference.
# "event" is structlog's default; celery task failures land under "error".
_MESSAGE_KEYS = ("event", "message", "error")

# Volatile tokens that make otherwise identical messages look unique. Bounded by
# non-alphanumerics rather than \b so that identifiers embedded in longer names
# ("subscription_<hex32>_offering_<hex32>_resource") are matched too.
_VOLATILE_PATTERNS = (
    (
        re.compile(
            r"(?<![0-9a-zA-Z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-zA-Z])"
        ),
        "<uuid>",
    ),
    (re.compile(r"(?<![0-9a-zA-Z])[0-9a-fA-F]{16,}(?![0-9a-zA-Z])"), "<hex>"),
)


def parse_structlog_message(msg):
    """Recover the message and context from a structlog-rendered log record.

    Returns a (message, context) pair when msg is a structlog event dict, where
    context holds the remaining keys. Returns None when msg is an ordinary log
    message that needs no unwrapping.
    """
    if isinstance(msg, dict):
        data = msg
    elif isinstance(msg, str):
        stripped = msg.strip()
        # Cheap guard so ast.literal_eval is not attempted on every log line.
        if not (stripped.startswith("{") and stripped.endswith("}")):
            return None
        try:
            data = ast.literal_eval(stripped)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return None
        if not isinstance(data, dict):
            return None
    else:
        return None

    for key in _MESSAGE_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value, {k: v for k, v in data.items() if k != key}

    return None


def normalize_for_fingerprint(message):
    """Replace volatile identifiers so equivalent messages share a group key."""
    for pattern, placeholder in _VOLATILE_PATTERNS:
        message = pattern.sub(placeholder, message)
    return message


def before_send(event, hint):
    """Normalise structlog-rendered log events before they are sent to Sentry."""
    record = hint.get("log_record")
    if record is None:
        return event

    parsed = parse_structlog_message(getattr(record, "msg", None))
    if parsed is None:
        return event

    message, context = parsed

    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        logentry["message"] = message
        # Params belong to the dict repr we just discarded.
        logentry.pop("params", None)

    if context:
        extra = event.setdefault("extra", {})
        if isinstance(extra, dict):
            for key, value in context.items():
                extra.setdefault(key, value)

    event["fingerprint"] = [
        getattr(record, "name", "") or "",
        normalize_for_fingerprint(message),
    ]
    return event
