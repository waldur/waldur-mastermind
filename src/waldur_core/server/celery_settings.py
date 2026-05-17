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

# Memory management - restart workers after N tasks to prevent memory leaks
CELERY_WORKER_MAX_TASKS_PER_CHILD = 100

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
