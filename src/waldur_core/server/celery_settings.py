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

# Prevent Celery from auto-declaring exchanges/queues to avoid type conflicts
# Only use explicitly defined queues and exchanges
CELERY_CREATE_MISSING_QUEUES = False
# If queues are auto-created, use topic exchanges (consistent with our config)
CELERY_TASK_CREATE_MISSING_QUEUES_EXCHANGE_TYPE = "topic"

# Regular tasks
CELERY_BEAT_SCHEDULE = {
    "pull-service-properties": {
        "task": "waldur_core.structure.ServicePropertiesListPullTask",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "pull-service-resources": {
        "task": "waldur_core.structure.ServiceResourcesListPullTask",
        # Pull resources strictly at the beginning of an hour
        "schedule": crontab(minute=0),
        "args": (),
    },
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
    "cancel-expired-invitations": {
        "task": "waldur_core.users.cancel_expired_invitations",
        "schedule": timedelta(hours=24),
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
    # Software catalog updates - run once daily at 3 AM
    "update-software-catalogs": {
        "task": "marketplace.update_software_catalogs",
        "schedule": crontab(hour=3, minute=0),
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
