import threading
from contextlib import contextmanager

_locals = threading.local()


def set_current_user(user):
    _locals.user = user


def get_current_user():
    return getattr(_locals, "user", None)


def set_skip_side_effects(skip=True):
    """
    Set flag to skip side effects during bulk operations in current thread.

    This flag is used during bulk operations like imports to prevent:
    - RabbitMQ message publishing
    - Signal handlers from running
    - Billing calculations
    - Other side effects during data migrations
    """
    _locals.skip_side_effects = skip


def get_skip_side_effects():
    """
    Get flag indicating if side effects should be skipped during bulk operations.

    Returns True during bulk operations like imports to prevent side effects.
    """
    return getattr(_locals, "skip_side_effects", False)


@contextmanager
def skip_side_effects():
    """
    Context manager to temporarily skip side effects during bulk operations.

    Used during bulk operations like imports to prevent:
    - RabbitMQ message publishing
    - Signal handlers from running
    - Billing calculations
    - Other side effects during data migrations
    """
    previous_value = get_skip_side_effects()
    set_skip_side_effects(True)
    try:
        yield
    finally:
        set_skip_side_effects(previous_value)
