import copy
import json

import pkg_resources
from django.conf import settings
from django.test import override_settings

backend_node_response = json.loads(
    pkg_resources.resource_stream(__name__, "backend_node.json").read().decode()
)


def override_plugin_settings(**kwargs):
    os_settings = copy.deepcopy(settings.WALDUR_RANCHER)
    os_settings.update(kwargs)
    return override_settings(WALDUR_RANCHER=os_settings)
