import datetime
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import traceback

from celery import current_app
from constance import config


class EventFormatter(logging.Formatter):
    def format_timestamp(self, time):
        return datetime.datetime.fromtimestamp(time, datetime.UTC).isoformat() + "Z"

    def levelname_to_importance(self, levelname):
        if levelname == "DEBUG":
            return "low"
        elif levelname == "INFO":
            return "normal"
        elif levelname == "WARNING":
            return "high"
        elif levelname == "ERROR":
            return "very high"
        else:
            return "critical"

    def format(self, record):
        message = {
            # basic
            "@timestamp": self.format_timestamp(record.created),
            "@version": 1,
            "message": record.getMessage(),
            # logging details
            "levelname": record.levelname,
            "logger": record.name,
            "importance": self.levelname_to_importance(record.levelname),
            "importance_code": record.levelno,
        }

        if hasattr(record, "event_type"):
            message["event_type"] = record.event_type

        if hasattr(record, "event_context"):
            message.update(record.event_context)

        return json.dumps(message)


class EventLoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger):
        super().__init__(logger, {})

    def process(self, msg, kwargs):
        if "extra" in kwargs:
            kwargs["extra"]["event"] = True
        else:
            kwargs["extra"] = {"event": True}
        return msg, kwargs


class RequireEvent(logging.Filter):
    """A filter that allows only event records."""

    def filter(self, record):
        return getattr(record, "event", False)


class RequireNotEvent(logging.Filter):
    """A filter that allows only non-event records."""

    def filter(self, record):
        return not getattr(record, "event", False)


class RequireNotBackgroundTask(logging.Filter):
    """Filter out messages from background tasks"""

    def filter(self, record):
        try:
            name = getattr(record, "data", {})["name"]
            task = current_app.tasks[name]
        except KeyError:
            return True
        is_background = getattr(task, "is_background", False)
        return not is_background


class TCPEventHandler(logging.handlers.SocketHandler):
    def __init__(self, host="localhost", port=5959):
        super().__init__(host, int(port))
        self.formatter = EventFormatter()

    def makePickle(self, record):
        return self.formatter.format(record).encode("utf-8") + b"\n"


_SENSITIVE_KEY_TERMS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "auth_token",
    "authorization",
    "credential",
    "client_secret",
    "kubeconfig",
)

# Text scrubber for log lines: matches "<term> = value" / "<term>: value".
_SENSITIVE_RE = re.compile(
    r"(?i)(" + "|".join(_SENSITIVE_KEY_TERMS) + r")(\s*[=:]\s*)(\S+)",
)

_SCRUBBED = "***"


def _is_sensitive_key(key) -> bool:
    return isinstance(key, str) and any(
        term in key.lower() for term in _SENSITIVE_KEY_TERMS
    )


def scrub_sensitive(value):
    """Recursively mask values under sensitive keys in a JSON-like structure.

    The text scrubber (_SENSITIVE_RE) only reaches log strings; this covers
    structured data such as event contexts / webhook payloads that are sent to
    external URLs, where a stray credential-named key would otherwise leak.
    """
    if isinstance(value, dict):
        return {
            key: (_SCRUBBED if _is_sensitive_key(key) else scrub_sensitive(val))
            for key, val in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub_sensitive(item) for item in value]
    return value


class DatabaseLogHandler(logging.Handler):
    """
    Custom logging handler that stores log records in PostgreSQL.

    Detects source (api/worker/beat) and instance (pod/container name).
    Buffers log records and bulk inserts to reduce DB load.
    """

    _ENABLED_CHECK_INTERVAL = 60  # seconds between constance lookups
    _MAX_BUFFER_SIZE = 10000  # hard cap on buffer; trimmed to _buffer_size
    _MAX_MESSAGE_LENGTH = 10000
    _MAX_TRACEBACK_LENGTH = 5000
    _SystemLog = None  # lazy reference to avoid AppRegistryNotReady

    def __init__(self, source=None, buffer_size=100, flush_interval=5):
        super().__init__(level=logging.INFO)
        self._source = source
        self._instance = None
        self._detected_source = None
        self._buffer = []
        self._buffer_lock = threading.Lock()
        self._buffer_size = buffer_size
        self._last_flush = time.time()
        self._flush_interval = flush_interval
        self._enabled = True
        self._enabled_last_check = 0.0
        self._apps_ready = False

    @property
    def instance(self):
        """Get pod/container hostname (cached)."""
        if self._instance is None:
            # $HOSTNAME in K8s contains pod name
            # In Docker, it's the container ID or explicit hostname
            self._instance = os.environ.get("HOSTNAME") or socket.gethostname()
        return self._instance

    @property
    def source(self):
        """Auto-detect source if not explicitly set."""
        if self._source:
            return self._source

        if self._detected_source:
            return self._detected_source

        # Check command line args for mastermind/worker/beat
        args = " ".join(sys.argv).lower()
        if "beat" in args:
            self._detected_source = "beat"
        elif "worker" in args:
            self._detected_source = "worker"
        else:
            self._detected_source = "api"

        return self._detected_source

    def _scrub(self, text):
        """Replace values of sensitive keys (password, token, etc.) with '***'."""
        if not text:
            return text
        return _SENSITIVE_RE.sub(r"\1\2***", text)

    def _check_apps_ready(self):
        """Check if Django apps are ready, caching the result once True."""
        if self._apps_ready:
            return True
        try:
            from django.apps import (
                apps,  # noqa: E402 — must be lazy; module loads before Django
            )

            if apps.ready:
                self._apps_ready = True
                return True
        except Exception:
            pass
        return False

    @classmethod
    def _get_model(cls):
        """Lazy-load SystemLog model, caching the class reference."""
        if cls._SystemLog is None:
            from waldur_core.logging.models import (
                SystemLog,  # noqa: E402 — must be lazy; circular import
            )

            cls._SystemLog = SystemLog
        return cls._SystemLog

    def emit(self, record):
        """Buffer log record for later bulk insert."""
        if not self._check_apps_ready():
            return

        now = time.time()
        if now - self._enabled_last_check > self._ENABLED_CHECK_INTERVAL:
            try:
                self._enabled = config.SYSTEM_LOG_ENABLED
            except Exception:
                pass  # DB may not be ready during startup
            self._enabled_last_check = now

        if not self._enabled:
            return

        try:
            message = self._scrub(self.format(record))[: self._MAX_MESSAGE_LENGTH]
        except Exception:
            # Malformed log records (e.g. mismatched %s args) must not crash
            message = self._scrub(str(record.msg))[: self._MAX_MESSAGE_LENGTH]

        log_entry = {
            "created": datetime.datetime.fromtimestamp(record.created, datetime.UTC),
            "source": self.source,
            "instance": self.instance,
            "level": record.levelname,
            "level_number": record.levelno,
            "logger_name": record.name,
            "message": message,
            "context": self._build_context(record),
        }

        should_flush = False
        records_to_flush = []

        with self._buffer_lock:
            self._buffer.append(log_entry)

            if len(self._buffer) > self._MAX_BUFFER_SIZE:
                self._buffer = self._buffer[-self._buffer_size :]

            should_flush = (
                len(self._buffer) >= self._buffer_size
                or time.time() - self._last_flush > self._flush_interval
            )
            if should_flush:
                records_to_flush = self._buffer[:]
                self._buffer.clear()
                self._last_flush = time.time()

        if should_flush:
            self._write_to_db(records_to_flush)

    def _build_context(self, record):
        """Extract exception and location context."""
        context = {}
        if record.exc_info:
            tb = self._scrub("".join(traceback.format_exception(*record.exc_info)))
            context["traceback"] = tb[: self._MAX_TRACEBACK_LENGTH]
        if record.pathname:
            context["pathname"] = record.pathname
        if record.lineno:
            context["lineno"] = record.lineno
        if record.funcName:
            context["funcName"] = record.funcName
        return context

    def _write_to_db(self, records):
        """Bulk insert records into the database. Called outside the buffer lock."""
        try:
            model = self._get_model()
            model.objects.bulk_create([model(**record) for record in records])
        except Exception:
            pass  # Avoid logging loop

    def close(self):
        """Flush remaining records on shutdown."""
        with self._buffer_lock:
            records = self._buffer[:]
            self._buffer.clear()
        if records:
            self._write_to_db(records)
        super().close()
