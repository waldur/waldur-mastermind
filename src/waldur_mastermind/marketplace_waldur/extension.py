from waldur_core.core import WaldurExtension


class MarketplaceWaldurExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_waldur'

    @staticmethod
    def is_assembly():
        return True
