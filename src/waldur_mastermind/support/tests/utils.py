import copy

from django.conf import settings
from django.test import override_settings


def override_plugin_settings(**kwargs):
    os_settings = copy.deepcopy(settings.WALDUR_SUPPORT)
    os_settings.update(kwargs)
    return override_settings(WALDUR_SUPPORT=os_settings)
