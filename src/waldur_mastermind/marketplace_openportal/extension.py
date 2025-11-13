from waldur_core.core import WaldurExtension


class MarketplaceOpenPortalExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.marketplace_openportal"

    @staticmethod
    def is_assembly():
        return True
