import copy

from django.conf import settings
from django.test import override_settings

from waldur_rancher.enums import AGENT_ROLE, SERVER_ROLE


def override_plugin_settings(**kwargs):
    os_settings = copy.deepcopy(settings.WALDUR_RANCHER)
    os_settings.update(kwargs)
    return override_settings(WALDUR_RANCHER=os_settings)


def format_nodes(default_conf: dict, server_count: int, agent_count: int):
    return [dict(**default_conf, role=SERVER_ROLE) for _ in range(server_count)] + [
        dict(**default_conf, role=AGENT_ROLE) for _ in range(agent_count)
    ]
