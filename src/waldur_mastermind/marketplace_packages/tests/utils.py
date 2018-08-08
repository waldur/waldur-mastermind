import copy

from django.conf import settings
from django.test import override_settings


def override_marketplace_packages_settings(**kwargs):
    support_settings = copy.deepcopy(settings.WALDUR_MARKETPLACE_PACKAGES)
    support_settings.update(kwargs)
    return override_settings(WALDUR_MARKETPLACE_PACKAGES=support_settings)
