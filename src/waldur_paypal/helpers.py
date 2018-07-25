import copy

from django.conf import settings
from django.test import override_settings

from . import models


def override_paypal_settings(**kwargs):
    plugin_settings = copy.deepcopy(settings.WALDUR_PAYPAL)
    plugin_settings.update(kwargs)
    return override_settings(WALDUR_PAYPAL=plugin_settings)


def convert_unit_of_measure(unit):
    if unit == 'quantity':
        return models.InvoiceItem.UnitsOfMeasure.QUANTITY
    else:
        return models.InvoiceItem.UnitsOfMeasure.AMOUNT


def get_invoice_item_quantity(item):
    if item.unit == 'quantity':
        return item.quantity
    else:
        return item.usage_days
