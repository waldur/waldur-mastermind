import math
from decimal import Decimal

from waldur_mastermind.marketplace import registrators as marketplace_registrators

from . import PLUGIN_NAME


class SlurmRegistrator(marketplace_registrators.MarketplaceRegistrator):
    plugin_name = PLUGIN_NAME

    @classmethod
    def convert_quantity(cls, usage: int | float | Decimal, component_type: str) -> int:
        minutes_in_hour = 60
        usage_float = float(usage)
        if component_type in ["ram", "mem"]:
            mb_in_gb = 1024
            quantity = int(math.ceil(usage_float / mb_in_gb / minutes_in_hour))
        else:
            quantity = int(math.ceil(usage_float / minutes_in_hour))
        return quantity
