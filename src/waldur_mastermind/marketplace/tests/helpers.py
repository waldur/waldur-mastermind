import copy

from django.conf import settings
from django.test import override_settings


def override_marketplace_settings(**kwargs):
    marketplace_settings = copy.deepcopy(settings.WALDUR_MARKETPLACE)
    marketplace_settings.update(kwargs)
    return override_settings(WALDUR_MARKETPLACE=marketplace_settings)
