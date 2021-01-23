from waldur_mastermind.marketplace.registrators import MarketplaceRegistrator

from . import PLUGIN_NAME


class SupportRegistrator(MarketplaceRegistrator):
    plugin_name = PLUGIN_NAME
