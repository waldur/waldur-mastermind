from waldur_core.core import WaldurExtension


class AuthSocialExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_auth_social'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns
