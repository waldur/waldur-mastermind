"""Local dev settings for the Matrix chat stack.

Extends dev_settings but runs Celery tasks synchronously, because the local
dev stack starts only `runserver` (no broker/worker). Matrix room creation,
member sync, history export, etc. are dispatched via `.delay()`, so without a
worker they would never run. Eager mode executes them inline instead.
"""

from waldur_core.server.dev_settings import *  # noqa
from waldur_core.server.dev_settings import ALLOWED_HOSTS

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = False

# Tuwunel (running in Docker) delivers appservice webhooks to Waldur via
# `host.docker.internal:10780`. ALLOWED_HOSTS must include that hostname
# or Django's CommonMiddleware rejects the PUT with 400 DisallowedHost.
ALLOWED_HOSTS = ALLOWED_HOSTS + ["host.docker.internal"]
