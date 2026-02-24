# Logging

## Structured logging (structlog)

Waldur uses [structlog](https://www.structlog.org/) via [django-structlog](https://django-structlog.readthedocs.io/) for structured logging. All logs are emitted as JSON in production (or readable console output in development when `WALDUR_DEV_LOGS=1`).

Existing stdlib logging calls work without changes: `logging.getLogger(__name__)` and `logger.info("Order %s created", order.uuid)` are processed through structlog's `foreign_pre_chain` and produce structured output with timestamp, level, logger name, request_id, user_uuid (in HTTP context), etc.

Example JSON output:

```json
{"event": "Order abc-123-def has been created.", "timestamp": "2025-02-18T14:30:00.123456Z", "level": "info", "logger": "waldur_mastermind.marketplace.views", "request_id": "3a8f801c-3fc5-4257-a78a-9a567c937561", "user_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"}
```

### Configuration

- **Console**: JSON (default) or colored console output when `WALDUR_DEV_LOGS=1`
- **Database**: SystemLog table receives JSON messages via `DatabaseLogHandler`
- **Celery**: Workers use structlog with task context (request_id, task_id)

Example Celery task log:

```json
{"event": "Order abc-123 sync completed.", "timestamp": "2025-02-18T14:31:00.456789Z", "level": "info", "logger": "waldur_mastermind.marketplace.tasks", "task_id": "6b11fd80-3cdf-4de5-acc2-3fd4633aa654", "request_id": "3a8f801c-3fc5-4257-a78a-9a567c937561", "user_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"}
```

### Adding structured fields

For explicit structured fields (e.g. for log aggregation queries), use `extra`:

```python
logger.info("Order created", extra={"order_uuid": str(order.uuid)})
```

The `event_logger` (see below) automatically includes `event_type` and `event_context` in logs.

### Customizing logging

Override `LOGGING` in your `settings.py` to add file output, syslog, or external aggregators. Extend the base config (e.g. via `copy.deepcopy`) rather than replacing it entirely. The snippets below can be combined.

```python
import copy
import logging.handlers

from waldur_core.server.base_settings import LOGGING as BASE_LOGGING

LOGGING = copy.deepcopy(BASE_LOGGING)

# Add file handler (JSON, suitable for log aggregators)
LOGGING["handlers"]["file"] = {
    "class": "logging.handlers.WatchedFileHandler",
    "filename": "/var/log/waldur/app.log",
    "formatter": "structlog_json",
}
LOGGING["root"]["handlers"].append("file")

# Optional: forward to syslog (e.g. for centralized logging)
# LOGGING["handlers"]["syslog"] = {
#     "class": "logging.handlers.SysLogHandler",
#     "address": "/dev/log",  # or ("logserver.example.com", 514) for remote
#     "facility": logging.handlers.SysLogHandler.LOG_LOCAL0,
#     "formatter": "structlog_json",
# }
# LOGGING["root"]["handlers"].append("syslog")
```

**Event-only forwarding to a log server** (e.g. for audit pipelines): use filters `RequireEvent` / `RequireNotEvent` from `waldur_core.logging.log` to separate events from general logs:

```python
LOGGING["filters"] = {
    "is-event": {"()": "waldur_core.logging.log.RequireEvent"},
    "is-not-event": {"()": "waldur_core.logging.log.RequireNotEvent"},
}
LOGGING["handlers"]["events_tcp"] = {
    "class": "waldur_core.logging.log.TCPEventHandler",
    "host": "logserver.example.com",
    "port": 5959,
    "filters": ["is-event"],
}
# Note: TCPEventHandler uses its own JSON formatter; it does not support external formatters.
```

**Per-logger level overrides**:

```python
LOGGING["loggers"]["waldur_core"] = {"level": "DEBUG"}
LOGGING["loggers"]["djangosaml2"] = {"level": "DEBUG"}
```

---

## Event logging

Event log entries is something an end user will see. In order to improve user experience the messages should be written in a consistent way.

Here are the guidelines for writing good log events.

* Use present perfect passive for the message.

  **Right:** `Environment %s has been created.`

  **Wrong:** `Environment %s was created.`

* Build a proper sentence: start with a capital letter, end with a period.

  **Right:** `Environment %s has been created.`

  **Wrong:** `environment %s has been created`

* Include entity names into the message string.

  **Right:** `User %s has gained role of %s in project %s.`

  **Wrong:** `User has gained role in project.`

* Don't include too many details into the message string.

  **Right:** `Environment %s has been updated.`

  **Wrong:** `Environment has been updated with name: %s, description: %s.`

* Use the name of an entity instead of its `__str__`.

  **Right:** `event_logger.info('Environment %s has been updated.', env.name)`

  **Wrong:** `event_logger.info('Environment %s has been updated.', env)`

* Don't put quotes around names or entity types.

  **Right:** `Environment %s has been created.`

  **Wrong:** `Environment "%s" has been created.`

* Don't capitalize entity types.

  **Right:** `User %s has gained role of %s in project %s.`

  **Wrong:** `User %s has gained Role of %s in Project %s.`

* For actions that require background processing log both start of the process and its outcome.

  **Success flow:**

   1. log `Environment %s creation has been started.` within HTTP request handler;

   2. log `Environment %s has been created.` at the end of background task.

  **Failure flow:**

   1. log `Environment %s creation has been started.` within HTTP request handler;

   2. log `Environment %s creation has failed.` at the end of background task.

* For actions that can be processed within HTTP request handler log only success.

  **Success flow:**

   log `User %s has been created.` at the end of HTTP request handler.

  **Failure flow:**

   don't log anything, since most of the errors that could happen here
   are validation errors that would be corrected by user and then resubmitted.
