import math

from waldur_mastermind.marketplace import registrators as marketplace_registrators

from . import PLUGIN_NAME


class SlurmRegistrator(marketplace_registrators.MarketplaceRegistrator):
    plugin_name = PLUGIN_NAME

    @classmethod
    def convert_usage_quantity(cls, usage, component_type):
        minutes_in_hour = 60
        if component_type == 'ram':
            mb_in_gb = 1024
            quantity = int(math.ceil(1.0 * usage / mb_in_gb / minutes_in_hour))
        else:
            quantity = int(math.ceil(1.0 * usage / minutes_in_hour))
        return quantity
