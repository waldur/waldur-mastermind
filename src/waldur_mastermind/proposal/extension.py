from waldur_core.core import WaldurExtension


class ProposalExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.proposal"

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in
