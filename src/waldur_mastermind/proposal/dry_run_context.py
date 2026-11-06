"""Whether the workflow engine runs inside a dry run, and what collects it.

A dry run (``dry_run.py``) drives the real engine on a synthetic proposal
inside a transaction that is always rolled back. The engine asks here where
its side effects go: while a collector is set, mail is handed to it instead
of being queued, and allocation is recorded instead of provisioned. Kept apart
from ``dry_run.py`` so the engine modules can ask without importing it.
"""

import contextlib
import contextvars

_collector = contextvars.ContextVar("proposal_workflow_dry_run", default=None)


def current():
    """The collector of the dry run in progress, or None outside one."""
    return _collector.get()


@contextlib.contextmanager
def collecting(collector):
    token = _collector.set(collector)
    try:
        yield collector
    finally:
        _collector.reset(token)
