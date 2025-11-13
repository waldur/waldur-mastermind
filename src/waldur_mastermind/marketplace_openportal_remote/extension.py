from waldur_core.core import WaldurExtension


class MarketplaceOpenPortalRemoteExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.marketplace_openportal_remote"

    @staticmethod
    def is_assembly():
        return True
