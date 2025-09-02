from waldur_mastermind.marketplace import registrators as marketplace_registrators

from ..marketplace.enums import SITE_AGENT_OFFERING


class RemoteSlurmRegistrator(marketplace_registrators.MarketplaceRegistrator):
    plugin_name = SITE_AGENT_OFFERING
