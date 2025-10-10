import warnings
from datetime import timedelta

from celery.schedules import crontab
from kombu import Queue

from waldur_core.core import WaldurExtension

CELERY_TASK_QUEUES = [
    Queue("tasks", exchange="tasks"),
    Queue("heavy", exchange="heavy"),
    Queue("background", exchange="background"),
]
CELERY_TASK_DEFAULT_QUEUE = "tasks"
CELERY_TASK_ROUTES = ("waldur_core.server.celeryconf.PriorityRouter",)
CELERY_TRACK_STARTED = True
CELERY_SEND_EVENTS = True

# Fix for Celery 6.0 deprecation warning
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

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
    "cancel-expired-invitations": {
        "task": "waldur_core.users.cancel_expired_invitations",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "cancel_expired_group_invitations": {
        "task": "waldur_core.users.cancel_expired_group_invitations",
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
