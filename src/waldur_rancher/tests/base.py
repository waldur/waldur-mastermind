import copy

from django.conf import settings
from django.test.utils import override_settings


def override_rancher_settings(**kwargs):
    rancher_settings = copy.deepcopy(settings.WALDUR_RANCHER)
    rancher_settings.update(kwargs)
    return override_settings(WALDUR_RANCHER=rancher_settings)
