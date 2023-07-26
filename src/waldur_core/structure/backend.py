import functools
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


def log_backend_action(action=None):
    """Logging for backend method.

    Expects django model instance as first argument.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapped(self, instance, *args, **kwargs):
            action_name = func.__name__.replace('_', ' ') if action is None else action

            logger.debug(
                'About to %s `%s` (PK: %s).', action_name, instance, instance.pk
            )
            result = func(self, instance, *args, **kwargs)
            logger.debug(
                'Action `%s` was executed successfully for `%s` (PK: %s).',
                action_name,
                instance,
                instance.pk,
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
        self.pull_subresources()

    def pull_service_properties(self):
        pass

    def pull_resources(self):
        pass

    def pull_subresources(self):
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
        ).values_list('backend_id', flat=True)
        result = []
        for remote_resource in remote_resources:
            if remote_resource['backend_id'] in local_backend_ids:
                continue
            result.append(remote_resource)
        return result

    def get_expired_resources(self, resource_model, remote_resources_ids):
        local_resources = resource_model.objects.filter(service_settings=self.settings)
        result = []
        for resource in local_resources:
            if resource.backend_id not in remote_resources_ids:
                result.append(resource)
        return result
