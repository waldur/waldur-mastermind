from decimal import Decimal

from waldur_mastermind.marketplace import registrators as marketplace_registrators
from waldur_mastermind.marketplace.utils import convert_slurm_usage

from ..marketplace.enums import SLURM_OFFERING


class SlurmRegistrator(marketplace_registrators.MarketplaceRegistrator):
    plugin_name = SLURM_OFFERING

    @classmethod
    def convert_quantity(cls, usage: int | float | Decimal, component_type: str) -> int:
        return convert_slurm_usage(usage, component_type)
