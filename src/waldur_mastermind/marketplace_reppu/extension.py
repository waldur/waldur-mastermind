from waldur_core.core import WaldurExtension


class MarketplaceReppuExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.marketplace_reppu"
