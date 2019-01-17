from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class MarketplaceSupportExtension(WaldurExtension):
    class Settings:
        WALDUR_MARKETPLACE_SUPPORT = {
            'REQUEST_LINK_TEMPLATE': 'https://www.example.com/#/offering/{request_uuid}/'
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_support'

    @staticmethod
    def is_assembly():
        return True
