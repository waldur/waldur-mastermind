import copy

from django.conf import settings
from django.test import override_settings


def override_paypal_settings(**kwargs):
    invoice_settings = copy.deepcopy(settings.WALDUR_PAYPAL)
    invoice_settings.update(kwargs)
    return override_settings(WALDUR_PAYPAL=invoice_settings)
