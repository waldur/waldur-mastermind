from waldur_mastermind.marketplace import registrators as marketplace_registrators

from ..marketplace.enums import SCRIPT_OFFERING


class ScriptRegistrator(marketplace_registrators.MarketplaceRegistrator):
    plugin_name = SCRIPT_OFFERING
