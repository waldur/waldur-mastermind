import copy
import importlib.resources
import json

from django.conf import settings
from django.test import override_settings

with importlib.resources.open_text("waldur_rancher.tests", "backend_node.json") as f:
    backend_node_response = json.loads(f.read())


def override_plugin_settings(**kwargs):
    os_settings = copy.deepcopy(settings.WALDUR_RANCHER)
    os_settings.update(kwargs)
    return override_settings(WALDUR_RANCHER=os_settings)
