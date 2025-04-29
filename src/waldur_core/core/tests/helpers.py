import copy
import importlib.resources
import json

from django.conf import settings
from django.test.utils import override_settings


def override_waldur_core_settings(**kwargs):
    waldur_settings = copy.deepcopy(settings.WALDUR_CORE)
    waldur_settings.update(kwargs)
    return override_settings(WALDUR_CORE=waldur_settings)


def load_json_resource(path, root):
    if root.startswith("src"):
        parts = root.split(".")
        root = ".".join(parts[1:-1])
    with importlib.resources.files(root).joinpath(path).open() as fp:
        return json.load(fp)
