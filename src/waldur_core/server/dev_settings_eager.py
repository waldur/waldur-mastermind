from waldur_core.server.dev_settings import *  # noqa: F401,F403

# Run Celery tasks inline: the dev stack has no worker, so order processing
# would otherwise never happen.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = False
