"""Which email log rows a workflow mail wrote as it went out.

``deliver_all`` sends each mail inside ``capture_email_logs`` and keeps the
rows written meanwhile on the mail's event, so the call team can open the
mail as it was sent from the proposal's timeline.
"""

import contextlib
import contextvars

from django.db.models.signals import post_save

from waldur_core.logging.models import EmailLog

_captured: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "proposal_captured_email_logs", default=None
)


def _record(sender, instance, created, **kwargs):
    bucket = _captured.get()
    if created and bucket is not None:
        bucket.append(instance.uuid.hex)


post_save.connect(_record, sender=EmailLog, dispatch_uid="proposal_capture_email_logs")


@contextlib.contextmanager
def capture_email_logs():
    """Collect the uuids of the email log rows written inside the block."""
    bucket: list[str] = []
    token = _captured.set(bucket)
    try:
        yield bucket
    finally:
        _captured.reset(token)
