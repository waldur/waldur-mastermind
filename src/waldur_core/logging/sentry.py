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
from contextvars import ContextVar

from django.conf import settings
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

# Populated by the deployment settings file (before Django finishes loading
# settings) via configure_constance_config() / event_scrubber(CONSTANCE_CONFIG);
# once Django is configured, falls back to settings.CONSTANCE_CONFIG.
_constance_config: dict = {}
_secret_keys_cache: frozenset[str] | None = None


def configure_constance_config(constance_config) -> None:
    """Register CONSTANCE_CONFIG for secret-key scrubbing before Django setup."""
    global _constance_config, _secret_keys_cache
    _constance_config = constance_config
    _secret_keys_cache = None


def _get_constance_config():
    if _constance_config:
        return _constance_config
    if settings.configured:
        return getattr(settings, "CONSTANCE_CONFIG", {})
    return {}


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
    """Normalise structlog-rendered log events and redact secrets before they
    are sent to Sentry."""
    return redact_option_secrets(
        redact_secrets(_normalise_log_event(event, hint), hint)
    )


def _normalise_log_event(event, hint):
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
        # sentry-sdk formats the record as the dict repr we just discarded,
        # context and secrets included. Params belong to it too.
        logentry["formatted"] = message
        logentry.pop("params", None)

    if context:
        _add_context(event.setdefault("extra", {}), context)

    event["fingerprint"] = [
        getattr(record, "name", "") or "",
        normalize_for_fingerprint(message),
    ]
    return event


def before_breadcrumb(crumb, hint):
    """Unwrap structlog-rendered log breadcrumbs as before_send does events.

    Lines below the event level become breadcrumbs of the next event, and
    sentry-sdk gives them the dict repr as their message, context included.
    """
    record = hint.get("log_record")
    if record is None:
        return crumb

    parsed = parse_structlog_message(getattr(record, "msg", None))
    if parsed is None:
        return crumb

    message, context = parsed
    crumb["message"] = message
    if context:
        _add_context(crumb.setdefault("data", {}), context)
    return crumb


def _add_context(target, context):
    if isinstance(target, dict):
        for key, value in _filter_secret_names(context).items():
            target.setdefault(key, value)


# Names of locals and fields that hold Matrix and SSO secrets. Sentry's default
# denylist matches whole names such as "token" and "secret", so these were sent
# in frame locals, e.g. the appservice token in every homeserver call.
_SECRET_NAMES = [
    "access_token",
    "openid_token",
    "refresh_token",
    "login_token",
    "as_token",
    "hs_token",
    "registration_secret",
    "registration_token",
    "client_secret",
    "api_secret",
    # A pre-shared key, such as an IPsec VPN's.
    "psk",
    "auth_token",
    "raw_token",
    # A re-authentication grant, as a request header and in request.META.
    "x-reauthentication-grant",
    "http_x_reauthentication_grant",
    # register_matrix_appservice: a homeserver admin's token, the bootstrap
    # admin's password and open sessions, and the key that creates admins.
    "admin_token",
    "bootstrap_password",
    "bootstrap_sessions",
    "shared_secret",
    # Matrix end-to-end encryption.
    "recovery_key",
    "pickle_key",
    "session_key",
    "temporary_password",
    "lease",
    "seed",
    "seeds",
    "cross_signing_seeds",
    # Secret storage and key backup: the key a recovery key decodes to, what
    # HKDF derives from it, and the backup's private key.
    "secret_key",
    "derived",
    "private_key",
    # Room history the bot decrypts for an export, and attachment keys.
    "decrypted",
    "plaintext",
    "export_messages",
    "export_message",
    "export_event",
    "export_page",
    "export_data",
    "json_content",
    "export_record",
    "export_line",
    "prepared_export",
    "spool_key",
    "encrypted_file",
    "encrypted_files",
    "attachment_key",
    "data_key",
    # Room history the bot reads for a command (matrix_chat.history_reader),
    # and the support ticket drafts written from it (matrix_chat.tickets).
    "history_events",
    "history_event",
    "history_page",
    "history_chunk",
    "history_root",
    "history_context",
    "history_messages",
    "history_message",
    "plaintext_lines",
    "decrypted_content",
    "edited_content",
    "command_event",
    "command_content",
    "command_args",
    "transcript",
    "transcript_lines",
    "conversation",
    "conversation_lower",
    "prompt",
    "guarded",
    "guarded_texts",
    "guard_result",
    "model_response",
    "model_text",
    "model_json",
    "model_answer",
    "draft_fields",
    "draft_title",
    "draft_description",
    "draft_overrides",
    "issue_data",
    "ticket_preview",
    "preview_plain",
    "preview_html",
]


# Constance settings that hold secrets travel in dicts keyed by their names,
# such as the settings init_matrix_settings seeds from the environment. The
# scrubber compares keys whole and lowercased, so each such name is listed:
# every secret_field, and every name that says it is a secret.
_SECRET_SETTING_SUFFIXES = ("_TOKEN", "_SECRET", "_KEY", "_PASSWORD")


def is_secret_setting(key):
    """Whether the Constance setting `key` holds a secret."""
    options = _get_constance_config().get(key, ())
    is_secret_field = len(options) > 2 and options[2] == "secret_field"
    return is_secret_field or key.endswith(_SECRET_SETTING_SUFFIXES)


def _secret_setting_names():
    return [key.lower() for key in _get_constance_config() if is_secret_setting(key)]


def event_scrubber(constance_config=None):
    if constance_config is not None:
        configure_constance_config(constance_config)
    # Recursive: httpx and nio keep the Authorization header, and registration
    # keeps passwords, inside dicts.
    return EventScrubber(
        denylist=DEFAULT_DENYLIST + _SECRET_NAMES + _secret_setting_names(),
        recursive=True,
    )


def _get_secret_keys() -> frozenset[str]:
    global _secret_keys_cache
    if _secret_keys_cache is None:
        keys = frozenset(event_scrubber().denylist)
        # Only cache once Constance settings are available (either registered
        # explicitly or loaded via Django settings), so an early call before
        # settings finish initializing does not freeze an incomplete denylist.
        if _get_constance_config():
            _secret_keys_cache = keys
        return keys
    return _secret_keys_cache


def _filter_secret_names(value):
    """Filter by name what reaches the event after the scrubber has run.

    Not with the scrubber itself: the event is serialized by then, and the
    AnnotatedValue markers it leaves would make the send fail.
    """
    secret_keys = _get_secret_keys()
    if isinstance(value, dict):
        return {
            key: (
                "[Filtered]"
                if isinstance(key, str) and key.lower() in secret_keys
                else _filter_secret_names(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_filter_secret_names(item) for item in value]
    return value


# Secrets inside values that no name gives away: nio puts the appservice token
# into every request path, auth headers end up in plain strings, and the
# appservice registration carries both tokens as YAML. The scheme names also
# start ordinary text ("basic support backend", "Bearer token expired"), and
# Sentry sends source lines such as "as_token: str", so after them only a
# token-shaped value is taken: one with a digit, or longer than a word. String
# locals arrive as their repr, newlines escaped, so a value ends at a backslash
# and a YAML key has no word boundary before it ("\nas_token: …").
_TOKEN_SHAPED = r"(?=[^\s&'\"\\]*\d|[^\s&'\"\\]{20})"
_SECRET_VALUES = re.compile(
    rf"((?:access_token=)|(?:\b(?:Bearer|Basic)\s+{_TOKEN_SHAPED})"
    rf"|(?:(?:as|hs)_token:\s*{_TOKEN_SHAPED}))"
    r"[^\s&'\"\\]+",
    re.IGNORECASE,
)


def _redact(value):
    if isinstance(value, str):
        return _SECRET_VALUES.sub(r"\1[Filtered]", value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def redact_secrets(event, hint):
    """Replace token-shaped values anywhere in the event, transactions too."""
    if event is None:
        return None
    return _redact(event)


# Values of secret options (marketplace option type ``secret``) seen while
# handling the current request. Their option names are chosen by providers, so
# no name gives them away: the values themselves are redacted wherever they
# appear in an event (request body, frame locals, messages). Reset when each
# request starts (see waldur_core.logging.apps).
_request_secret_values: ContextVar[frozenset] = ContextVar(
    "waldur_request_secret_values", default=frozenset()
)
# A shorter secret is replaced only where it is a whole string, so that it
# does not mangle the rest of the event.
_MIN_SECRET_SUBSTRING_LENGTH = 8
# The request bodies whose "value" is a secret.
_SECRET_VALUE_PATHS = ("/set_option_secret/",)


def register_secret_value(value):
    """Have every event of the current request leave out ``value``."""
    if isinstance(value, str) and value:
        _request_secret_values.set(_request_secret_values.get() | {value})


def reset_secret_values(**kwargs):
    _request_secret_values.set(frozenset())


def _redact_values(value, secrets, long_secrets):
    if isinstance(value, str):
        if value in secrets:
            return "[Filtered]"
        for secret in long_secrets:
            if secret in value:
                value = value.replace(secret, "[Filtered]")
        return value
    if isinstance(value, dict):
        return {
            key: _redact_values(item, secrets, long_secrets)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_values(item, secrets, long_secrets) for item in value]
    return value


def redact_option_secrets(event):
    """Leave secret option values out of the event.

    The ``value`` of a set_option_secret request body is filtered by the path,
    and any value a secret option received in this request, by its content.
    """
    if event is None:
        return None
    request = event.get("request")
    if isinstance(request, dict) and any(
        path in str(request.get("url") or "") for path in _SECRET_VALUE_PATHS
    ):
        data = request.get("data")
        if isinstance(data, dict):
            request["data"] = {
                **data,
                **({"value": "[Filtered]"} if "value" in data else {}),
            }
        elif data:
            request["data"] = "[Filtered]"
    secrets = _request_secret_values.get()
    if not secrets:
        return event
    # Frame locals arrive as their repr, newlines and quotes escaped, so a
    # multiline secret is looked for in that form too.
    long_secrets = sorted(
        {
            form
            for secret in secrets
            if len(secret) >= _MIN_SECRET_SUBSTRING_LENGTH
            for form in (secret, repr(secret)[1:-1])
        },
        key=len,
        reverse=True,
    )
    return _redact_values(event, secrets, long_secrets)
