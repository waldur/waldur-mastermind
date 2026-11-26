# This will make sure the app is always imported when
# Django starts so that shared_task will use this app.
# Use an absolute import so importing this package under an alias (e.g.
# ``src.waldur_core.server`` during pytest collection) reuses the already
# loaded ``waldur_core.server.celeryconf`` module instead of creating a
# second Celery app instance and hijacking ``celery.current_app``.
from waldur_core.server.celeryconf import app as celery_app  # noqa: F401
