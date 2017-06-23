import copy

from django.conf import settings
from django.test import override_settings


def override_invoices_settings(**kwargs):
    nc_settings = copy.deepcopy(settings.INVOICES)
    nc_settings.update(kwargs)
    return override_settings(INVOICES=nc_settings)
