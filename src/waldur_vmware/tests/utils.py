import copy

from django.conf import settings
from django.test import override_settings


def override_plugin_settings(**kwargs):
    plugin_settings = copy.deepcopy(settings.WALDUR_VMWARE)
    plugin_settings.update(kwargs)
    return override_settings(WALDUR_VMWARE=plugin_settings)
