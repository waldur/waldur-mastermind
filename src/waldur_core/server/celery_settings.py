import socket
import warnings
from datetime import timedelta

from celery.schedules import crontab
from kombu import Exchange, Queue

from waldur_core.core import WaldurExtension

CELERY_TASK_QUEUES = [
    Queue(
        "tasks-durable",
        exchange=Exchange("tasks-durable", type="topic"),
        routing_key="tasks-durable",
        queue_arguments={"x-queue-type": "quorum"},
    ),
    Queue(
        "heavy-durable",
        exchange=Exchange("heavy-durable", type="topic"),
        routing_key="heavy-durable",
        queue_arguments={"x-queue-type": "quorum"},
    ),
    Queue(
        "background-durable",
        exchange=Exchange("background-durable", type="topic"),
        routing_key="background-durable",
        queue_arguments={"x-queue-type": "quorum"},
    ),
]
CELERY_TASK_DEFAULT_QUEUE = "tasks-durable"
CELERY_TASK_ROUTES = ("waldur_core.server.celeryconf.PriorityRouter",)
CELERY_TRACK_STARTED = True
CELERY_SEND_EVENTS = True

# Fix for Celery 6.0 deprecation warning
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Disable psycopg3 server-side prepared statements for the database result backend.
# psycopg3 caches prepared statements per-connection; PgBouncer in transaction
# pooling mode reassigns connections between transactions, so a prepared statement
# created on connection A is invisible on connection B — causing
# "prepared statement _pg3_N does not exist" errors.
CELERY_DATABASE_ENGINE_OPTIONS = {"connect_args": {"prepare_threshold": None}}

# Memory management - restart workers after N tasks to prevent memory leaks.
# Higher value reduces broker reconnect churn (each fork re-handshakes
# connections); see broker-resilience block below.
CELERY_WORKER_MAX_TASKS_PER_CHILD = 2000

# Time limits - prevent runaway tasks
CELERY_TASK_SOFT_TIME_LIMIT = (
    1200  # 20 minutes soft limit (raises SoftTimeLimitExceeded)
)
CELERY_TASK_TIME_LIMIT = 1800  # 30 minutes hard kill

# Prefetch - process one task at a time for better memory control
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# Result expiration - auto-delete old task results after 1 hour
CELERY_RESULT_EXPIRES = 3600

# Memory optimization for RabbitMQ connection pool
CELERY_BROKER_POOL_LIMIT = 10

# --- Broker resilience settings (CSCS-4VC / CSCS-4KB follow-up) ---
#
# Goal: detect dead/half-open AMQP connections inside gunicorn's worker
# timeout window so a stuck `recv()` waiting for a publish ACK can't ride out
# the whole 30s and trip `WORKER TIMEOUT` → `SystemExit`.
#
# Three layers of dead-connection detection, in increasing depth:
#
# 1. AMQP heartbeat (BROKER_HEARTBEAT). py-amqp sends a heartbeat frame at
#    half this interval; the broker considers the connection dead after two
#    missed heartbeats. So heartbeat=30s ⇒ ~30s detection budget on top of
#    AMQP idle.
#
# 2. TCP keepalive (socket_settings). The kernel sends SYN probes after
#    TCP_KEEPIDLE seconds of socket idle, then probes every TCP_KEEPINTVL
#    until TCP_KEEPCNT failures ⇒ socket closed at kernel level. This
#    catches half-open connections that AMQP heartbeats may miss because
#    they share the same broken socket. ~30s detection budget.
#
# 3. Connection-pool turnover (BROKER_POOL_LIMIT existing setting). A pool
#    of 10 connections per process turns over enough to keep individual
#    sockets fresh even without per-publish reconnect.
#
# We keep `confirm_publish: True` because durable invoice / billing writes
# need broker durability guarantees; the resilience settings above bound
# the worst-case wait that confirm-publish can introduce.
#
# Heartbeat: py-amqp ticks at half this interval; broker drops the
# connection after ~30s of missed heartbeats. Without this, kombu
# negotiates the broker's default (60s) and dead-connection detection
# takes 120s+ — well after gunicorn's 30s worker timeout.
#
# IMPORTANT: the top-level ``CELERY_BROKER_HEARTBEAT`` setting is
# silently ignored on the publisher path (Celery does not propagate it
# to ``app.broker_connection()`` / ``app.producer_pool``). It must be
# set via ``broker_transport_options["heartbeat"]`` to actually reach
# the kombu Connection.
#
# SO_KEEPALIVE is enabled unconditionally by py-amqp
# (amqp/transport.py:_init_socket). py-amqp also ships defaults of
# TCP_KEEPIDLE=60s, TCP_KEEPINTVL=10s, TCP_KEEPCNT=9 — too slow to fire
# before gunicorn's 30s worker timeout. The overrides below tighten the
# probe schedule to detect a dead socket in ~25s
# (KEEPIDLE + KEEPINTVL * KEEPCNT = 10 + 5*3 = 25s).
#
# ``socket_settings`` keys must be integer constants from the ``socket``
# module (py-amqp passes them straight to setsockopt(SOL_TCP, opt, val)).
# Some keys are Linux-only (TCP_KEEPIDLE in particular); we resolve them
# conditionally so the settings load on macOS dev too, even though the
# full effect requires Linux.
#
# What's NOT here: a per-publish ``confirm_timeout`` kwarg. py-amqp's
# Channel._basic_publish accepts it but kombu's Producer does not expose
# it through Celery's app config (see celery#9259). A future MR could
# add a custom Producer subclass to inject ``confirm_timeout=5`` on
# every publish; for now the heartbeat + keepalive combo bounds the
# worst-case wait at the transport layer.
_BROKER_SOCKET_SETTINGS = {}
for _opt, _val in (
    ("TCP_KEEPIDLE", 10),
    ("TCP_KEEPINTVL", 5),
    ("TCP_KEEPCNT", 3),
):
    _const = getattr(socket, _opt, None)
    if _const is not None:
        _BROKER_SOCKET_SETTINGS[_const] = _val

CELERY_BROKER_TRANSPORT_OPTIONS = {
    "confirm_publish": True,
    "heartbeat": 30,
    "socket_settings": _BROKER_SOCKET_SETTINGS,
}

# Prevent Celery from auto-declaring exchanges/queues to avoid type conflicts
# Only use explicitly defined queues and exchanges
CELERY_CREATE_MISSING_QUEUES = False
# If queues are auto-created, use topic exchanges (consistent with our config)
CELERY_TASK_CREATE_MISSING_QUEUES_EXCHANGE_TYPE = "topic"

# Default schedule interval for SCIM entitlement reconciliation task (in hours)
DEFAULT_SCIM_RECONCILIATION_SCHEDULE_HOURS = 1

# Regular tasks
CELERY_BEAT_SCHEDULE = {
    "check-expired-permissions": {
        "task": "waldur_core.permissions.check_expired_permissions",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "sync-user-deactivation-status": {
        "task": "waldur_core.permissions.sync_user_deactivation_status",
        "schedule": timedelta(hours=3),
        "args": (),
    },
    "reconcile-user-roles-against-availability": {
        "task": "waldur_core.permissions.reconcile_user_roles_against_availability",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "cancel-expired-invitations": {
        "task": "waldur_core.users.cancel_expired_invitations",
        "schedule": timedelta(hours=6),
        "args": (),
    },
    "send-reminder-for-pending-invitations": {
        "task": "waldur_core.users.send_reminder_for_pending_invitations",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "process-pending-project-invitations": {
        "task": "waldur_core.users.process_pending_project_invitations",
        "schedule": timedelta(hours=2),
        "args": (),
    },
    "core-reset-updating-resources": {
        "task": "waldur_core.reset_updating_resources",
        "schedule": timedelta(minutes=10),
        "args": (),
    },
    "marketplace-reset-stuck-updating-resources": {
        "task": "waldur_mastermind.marketplace.reset_stuck_updating_resources",
        "schedule": timedelta(minutes=10),
        "args": (),
    },
    "structure-set-erred-stuck-resources": {
        "task": "waldur_core.structure.SetErredStuckResources",
        "schedule": timedelta(hours=1),
        "args": (),
    },
    "create_customer_permission_reviews": {
        "task": "waldur_core.structure.create_customer_permission_reviews",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "create_project_permission_reviews": {
        "task": "waldur_core.structure.create_project_permission_reviews",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "send-project-digest-notifications": {
        "task": "waldur_core.structure.send_project_digest_notifications",
        "schedule": crontab(hour=8, minute=0),
        "args": (),
    },
    "update-custom-quotas": {
        "task": "waldur_core.quotas.update_custom_quotas",
        "schedule": timedelta(hours=1),
        "args": (),
    },
    "update-standard-quotas": {
        "task": "waldur_core.quotas.update_standard_quotas",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "cleanup-orphaned-answers": {
        "task": "waldur_core.checklist.cleanup_orphaned_answers",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "expire-stale-verifications": {
        "task": "waldur_core.onboarding.expire_stale_verifications",
        "schedule": timedelta(hours=1),
        "args": (),
    },
    "delete-old-verifications": {
        "task": "waldur_core.onboarding.delete_old_verifications",
        "schedule": timedelta(days=1),
        "args": (),
    },
    # Software catalog updates - run once daily at 3 AM
    "update-software-catalogs": {
        "task": "marketplace.update_software_catalogs",
        "schedule": crontab(hour=3, minute=0),
        "args": (),
    },
    # Software catalog cleanup - run once daily at 4 AM (after updates)
    "cleanup-software-catalogs": {
        "task": "marketplace.cleanup_old_software_catalogs",
        "schedule": crontab(hour=4, minute=0),
        "args": (),
    },
    # User actions - update every 6 hours
    "update-user-actions": {
        "task": "waldur_core.user_actions.update_user_actions",
        "schedule": crontab(hour="*/6", minute=0),
        "args": (),
    },
    # Cleanup expired silenced actions - daily at 2 AM
    "cleanup-expired-silenced-actions": {
        "task": "waldur_core.user_actions.cleanup_expired_silenced_actions",
        "schedule": crontab(hour=2, minute=0),
        "args": (),
    },
    # Reconcile stuck invitations that were never sent because of errors or celery downtime.
    "resend-stuck-invitations": {
        "task": "waldur_core.users.resend_stuck_invitations",
        "schedule": timedelta(hours=1),
        "args": (),
    },
    "scim-hourly-entitlement-reconciliation": {
        "task": "waldur_core.users.scim.sync_recent_entitlements",
        # Lookback window is automatically set to 2x this value in the task in the task itself
        "schedule": timedelta(hours=DEFAULT_SCIM_RECONCILIATION_SCHEDULE_HOURS),
        "args": (),
    },
    # Cleanup old action executions - weekly on Sunday at 1 AM
    "cleanup-old-action-executions": {
        "task": "waldur_core.user_actions.cleanup_old_action_executions",
        "schedule": crontab(hour=1, minute=0, day_of_week=0),
        "args": (),
    },
    # Send action digest notifications - daily at 9 AM
    "send-action-digest-notifications": {
        "task": "waldur_core.user_actions.send_action_digest_notifications",
        "schedule": crontab(hour=9, minute=0),
        "args": (),
    },
    # Cleanup dangling user actions - daily (fallback for missed deletions)
    "cleanup-dangling-user-actions": {
        "task": "waldur_core.user_actions.cleanup_dangling_user_actions",
        "schedule": crontab(hour=3, minute=30),
        "args": (),
    },
    # Cleanup system logs - enforce ~1MB size limit per source
    "cleanup-system-logs": {
        "task": "waldur_core.logging.cleanup_system_logs",
        "schedule": timedelta(minutes=15),
        "args": (),
    },
    # Cleanup site agent logs - enforce row count limit per agent identity
    "cleanup-site-agent-logs": {
        "task": "waldur_mastermind.marketplace_site_agent.cleanup_site_agent_logs",
        "schedule": timedelta(minutes=15),
        "args": (),
    },
    # Table growth monitoring - sample sizes daily at 1 AM
    "sample-table-sizes": {
        "task": "waldur_core.sample_table_sizes",
        "schedule": crontab(hour=1, minute=0),
        "args": (),
    },
    # Table growth monitoring - check alerts daily at 2 AM
    "check-table-growth-alerts": {
        "task": "waldur_core.check_table_growth_alerts",
        "schedule": crontab(hour=2, minute=0),
        "args": (),
    },
    # Cleanup expired personal access tokens every 6 hours
    "cleanup-expired-pats": {
        "task": "waldur_core.core.cleanup_expired_personal_access_tokens",
        "schedule": timedelta(hours=6),
        "args": (),
    },
    # Cleanup unredeemed token exchange codes every minute
    "cleanup-stale-token-exchange-codes": {
        "task": "waldur_core.core.cleanup_stale_token_exchange_codes",
        "schedule": timedelta(minutes=1),
        "args": (),
    },
}

for ext in WaldurExtension.get_extensions():
    for name, task in ext.celery_tasks().items():
        if name in CELERY_BEAT_SCHEDULE:
            warnings.warn(
                "Celery beat task %s from Waldur extension %s "
                "is overlapping with primary tasks definition"
                % (name, ext.django_app())
            )
        else:
            CELERY_BEAT_SCHEDULE[name] = task
