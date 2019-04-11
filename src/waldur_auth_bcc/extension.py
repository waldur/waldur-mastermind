from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class AuthBCCExtension(WaldurExtension):

    class Settings:
        WALDUR_AUTH_BCC = {
            'ENABLED': False,
            'BASE_API_URL': '',
            'USERNAME': '',
            'PASSWORD': '',
        }

    @staticmethod
    def django_app():
        return 'waldur_auth_bcc'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns
        return urlpatterns
