"""The settings Waldur can change at runtime, and their defaults.

The definitions live in ``waldur_core.server.constance_settings`` under their
``CONSTANCE_*`` names. They are read from ``django.conf.settings`` on
every call: deployments and test settings modules adjust ``CONSTANCE_CONFIG``
after it is imported.

Each ``CONSTANCE_CONFIG`` entry is ``(default, help_text[, field_type])``, where
``field_type`` is a Python type or a name from ``CONSTANCE_ADDITIONAL_FIELDS``.
"""

from django.conf import settings

SECRET_FIELD_TYPE = "secret_field"


def definitions():
    return settings.CONSTANCE_CONFIG


def additional_fields():
    return settings.CONSTANCE_ADDITIONAL_FIELDS


def fieldsets():
    return settings.CONSTANCE_CONFIG_FIELDSETS


def choices():
    return getattr(settings, "CONSTANCE_CONFIG_CHOICES", {}) or {}


def has(key):
    return key in settings.CONSTANCE_CONFIG


def default(key):
    return settings.CONSTANCE_CONFIG[key][0]


def help_text(key):
    return settings.CONSTANCE_CONFIG[key][1]


def field_type(key):
    """The declared type: a Python type, a field name, or None if undeclared."""
    definition = settings.CONSTANCE_CONFIG[key]
    if len(definition) > 2:
        return definition[2]
    return None


def is_secret(key):
    return field_type(key) == SECRET_FIELD_TYPE


def secret_keys():
    return frozenset(key for key in settings.CONSTANCE_CONFIG if is_secret(key))
