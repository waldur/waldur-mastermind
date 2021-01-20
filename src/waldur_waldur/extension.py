from waldur_core.core import WaldurExtension


class RemoteWaldurExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_waldur'

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in
