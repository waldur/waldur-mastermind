import importlib
from functools import lru_cache

from django.apps import apps

from waldur_core.structure.exceptions import ServiceBackendNotImplemented


def get_resource_type(model):
    return f'{get_service_type(model)}.{model._meta.object_name}'


def get_service_type(model):
    try:
        return model.get_service_name()
    except AttributeError:
        pass
    app_config = apps.get_containing_app_config(model.__module__)
    return getattr(app_config, 'service_name', None)


class SupportedServices:
    @classmethod
    def get_filter_mapping(cls):
        return {name: code for code, name in cls.get_choices()}

    _registry = {}

    @classmethod
    def register_backend(cls, backend_class):
        key = get_service_type(backend_class)
        cls._registry[key] = backend_class
        modname, _, clsname = backend_class.__module__.rpartition('.')
        importlib.import_module(modname + '.serializers')

    @classmethod
    def get_service_backend(cls, key):
        if not isinstance(key, str):
            key = get_service_type(key)
        try:
            return cls._registry[key]
        except KeyError:
            raise ServiceBackendNotImplemented

    @classmethod
    @lru_cache(maxsize=1)
    def get_choices(cls):
        items = [(code, code) for code in cls._registry.keys()]
        return sorted(items)

    @classmethod
    def has_service_type(cls, service_type):
        return service_type in cls._registry
