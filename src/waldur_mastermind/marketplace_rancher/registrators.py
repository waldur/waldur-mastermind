from waldur_mastermind.marketplace import registrators as marketplace_registrators

from ..marketplace.enums import RANCHER_OFFERING


class RancherRegistrator(marketplace_registrators.MarketplaceRegistrator):
    plugin_name = RANCHER_OFFERING
