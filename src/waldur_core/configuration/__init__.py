"""Settings that administrators change at runtime.

``config.<KEY>`` reads a setting and ``config.<KEY> = value`` writes it, for any
key declared in the registry (``waldur_core.configuration.registry``); other
names raise ``AttributeError``, so ``getattr(config, key, default)`` works.

The model is looked up through the app registry rather than imported: this
module is imported by models modules of other apps, before apps are loaded.
"""

from django.apps import apps as django_apps
from django.core.exceptions import AppRegistryNotReady

from waldur_core.configuration import registry, secrets


def _setting_model():
    return django_apps.get_model("configuration", "Setting")


class Config:
    def __getattr__(self, key):
        if not registry.has(key):
            raise AttributeError(key)
        try:
            model = _setting_model()
        except AppRegistryNotReady:
            if registry.is_secret(key):
                return secrets.undecryptable_value(key)
            return registry.default(key)
        return model.objects.get_value(key)

    def __setattr__(self, key, value):
        if not registry.has(key):
            raise AttributeError(key)
        _setting_model().objects.set(key, value, source="application")

    def __dir__(self):
        return list(registry.definitions())


config = Config()
