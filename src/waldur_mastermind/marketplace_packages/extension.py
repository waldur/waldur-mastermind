from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class MarketplacePackagesExtension(WaldurExtension):

    class Settings:
        WALDUR_MARKETPLACE_PACKAGES = {
            'CUSTOMER_ID': None,
            'CATEGORY_ID': None,
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_packages'

    @staticmethod
    def is_assembly():
        return True
