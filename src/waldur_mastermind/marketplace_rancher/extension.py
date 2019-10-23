from waldur_core.core import WaldurExtension


class MarketplaceRancherExtension(WaldurExtension):
    class Settings:
        pass

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_rancher'

    @staticmethod
    def is_assembly():
        return True
