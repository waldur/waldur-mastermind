from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class MarketplaceVolumeExtension(WaldurExtension):

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_volume'

    @staticmethod
    def is_assembly():
        return True
