from waldur_core.core import WaldurExtension


class MarketplacePromotionsExtension(WaldurExtension):
    class Settings:
        pass

    @staticmethod
    def django_app():
        return "waldur_mastermind.promotions"

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in
