from waldur_mastermind.marketplace.registrators import MarketplaceRegistrator

from ..marketplace.enums import SUPPORT_OFFERING


class SupportRegistrator(MarketplaceRegistrator):
    plugin_name = SUPPORT_OFFERING
