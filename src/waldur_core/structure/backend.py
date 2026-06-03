import contextvars
import functools
import logging
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# Set by `log_backend_action` while a decorated backend method is running so
# downstream observers (e.g. the OpenStack timed session) can tag each outgoing
# API call with the originating action name. Used for tracing slow tenant
# teardowns where dozens of HTTP calls fan out from a few backend methods.
current_backend_action: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "waldur_current_backend_action", default=None
)


def log_backend_action(action=None):
    """Logging for backend method.

    Expects django model instance as first argument.
    Logs elapsed time alongside the completion message and binds the action
    name into the `current_backend_action` ContextVar for the duration of the
    call so per-HTTP-call instrumentation can attribute calls to it.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapped(self, instance, *args, **kwargs):
            action_name = func.__name__.replace("_", " ") if action is None else action

            logger.debug(
                "About to %s `%s` (PK: %s).", action_name, instance, instance.pk
            )
            start = time.perf_counter()
            token = current_backend_action.set(action_name)
            try:
                result = func(self, instance, *args, **kwargs)
            except Exception:
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.warning(
                    "Action `%s` failed for `%s` (PK: %s) after %.0fms.",
                    action_name,
                    instance,
                    instance.pk,
                    elapsed_ms,
                )
                raise
            finally:
                current_backend_action.reset(token)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                "Action `%s` was executed successfully for `%s` (PK: %s) in %.0fms.",
                action_name,
                instance,
                instance.pk,
                elapsed_ms,
            )
            return result

        return wrapped

    return decorator


class ServiceBackend(ABC):
    """Basic service backed with only common methods pre-defined."""

    DEFAULTS = {}

    def __init__(self, settings, **kwargs):
        pass

    def validate_settings(self):
        self.ping(raise_exception=True)

    @abstractmethod
    def ping(self, raise_exception=False):
        pass

    def sync(self):
        self.pull_service_properties()
        self.pull_resources()

    def pull_service_properties(self):
        pass

    def pull_resources(self):
        pass

    def has_global_properties(self):
        return False

    @staticmethod
    def gb2mb(val):
        return int(val * 1024) if val else 0

    @staticmethod
    def tb2mb(val):
        return int(val * 1024 * 1024) if val else 0

    @staticmethod
    def mb2gb(val):
        return int(val / 1024) if val else 0

    @staticmethod
    def mb2tb(val):
        return int(val / 1024 / 1024) if val else 0

    @staticmethod
    def b2gb(val):
        return int(val / 1024 / 1024 / 1024) if val else 0

    def get_importable_resources(self, resource_model, remote_resources):
        local_backend_ids = resource_model.objects.filter(
            service_settings=self.settings
        ).values_list("backend_id", flat=True)
        result = []
        for remote_resource in remote_resources:
            if remote_resource["backend_id"] in local_backend_ids:
                continue
            result.append(remote_resource)
        return result
