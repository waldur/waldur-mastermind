"""Thread-local opt-in suppression for the generic CREATE/UPDATE_OF_*_CREDIT_BY_STAFF
audit events emitted by ``handlers.log_credit`` / ``handlers.log_project_credit``.

Used by programmatic flows (compensation, set_to_zero_overdue_credits,
refund-on-removal) that emit their own specialized credit-mutation events
(``REDUCTION_OF_*``, ``ROLL_BACK_*``, ``SET_TO_ZERO_OVERDUE_CREDIT``,
``AUTOMATIC_CREDIT_ADJUSTMENT``) and would otherwise produce duplicate audit
entries.

The previous mechanism — short-circuiting on ``update_fields`` in the post_save
signal — was leaky: any caller passing ``update_fields=["value"]`` (e.g. another
subsystem reconciling credits, or a one-off shell save) silently bypassed the
audit log, leading to material credit-value drift in production with no audit
record. Switching to an explicit opt-in keeps programmatic flows quiet but
guarantees every other value mutation is captured.

This lives in its own module to keep the import graph shallow: ``compensations``
and ``tasks`` both need it but cannot import ``handlers`` without triggering the
``marketplace.billing`` → ``structure.filters`` chain at app-load time.
"""

import contextlib
import threading

_state = threading.local()


@contextlib.contextmanager
def skip_credit_audit():
    """Suppress generic ``*_CREDIT_BY_STAFF`` audit events while the block runs.

    Use only in flows that emit their own specialized credit-mutation event.
    """
    previous = getattr(_state, "active", False)
    _state.active = True
    try:
        yield
    finally:
        _state.active = previous


def credit_audit_skipped() -> bool:
    return getattr(_state, "active", False)
