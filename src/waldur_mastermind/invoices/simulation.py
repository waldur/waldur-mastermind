"""Whether a billing simulation is running in this thread.

``marketplace.billing_simulation`` bills a hypothetical month inside a
transaction it rolls back. Signal handlers whose effects a rollback cannot
undo (a cache key, a request to an outside service) return early while
``is_active()`` is true. This module imports nothing so that any handler can
import it.
"""

import threading
from contextlib import contextmanager

_state = threading.local()


def is_active() -> bool:
    return getattr(_state, "active", False)


@contextmanager
def active():
    previous = is_active()
    _state.active = True
    try:
        yield
    finally:
        _state.active = previous
