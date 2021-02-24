from waldur_mastermind.marketplace import registrators as marketplace_registrators

from . import PLUGIN_NAME


class RancherRegistrator(marketplace_registrators.MarketplaceRegistrator):
    plugin_name = PLUGIN_NAME
