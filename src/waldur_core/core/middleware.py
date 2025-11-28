import threading
from contextlib import contextmanager

_locals = threading.local()


def set_current_user(user):
    _locals.user = user


def get_current_user():
    return getattr(_locals, "user", None)


def set_skip_rabbitmq_messages(skip=True):
    """Set flag to skip RabbitMQ messages in current thread."""
    _locals.skip_rabbitmq_messages = skip


def get_skip_rabbitmq_messages():
    """Get flag indicating if RabbitMQ messages should be skipped."""
    return getattr(_locals, "skip_rabbitmq_messages", False)


@contextmanager
def skip_rabbitmq_messages():
    """Context manager to temporarily skip RabbitMQ messages."""
    previous_value = get_skip_rabbitmq_messages()
    set_skip_rabbitmq_messages(True)
    try:
        yield
    finally:
        set_skip_rabbitmq_messages(previous_value)
