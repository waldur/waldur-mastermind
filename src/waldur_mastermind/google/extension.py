from waldur_core.core import WaldurExtension


class GoogleExtension(WaldurExtension):
    class Settings:
        WALDUR_GOOGLE = {
            'CLIENT_ID': '',
            'CLIENT_SECRET': '',
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.google'

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in
