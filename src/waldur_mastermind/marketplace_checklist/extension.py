from waldur_core.core import WaldurExtension


class MarketplaceChecklistExtension(WaldurExtension):

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_checklist'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def django_urls():
        from .urls import urlpatterns
        return urlpatterns
