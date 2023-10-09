from waldur_core.core import WaldurExtension


class LexisExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_lexis'

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in
