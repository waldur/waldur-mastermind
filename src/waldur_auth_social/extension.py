from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class AuthSocialExtension(WaldurExtension):

    class Settings:
        # wiki: https://opennode.atlassian.net/wiki/display/WD/AuthSocial+plugin+configuration
        WALDUR_AUTH_SOCIAL = {
            'GOOGLE_SECRET': '',
            'GOOGLE_CLIENT_ID': '',

            'FACEBOOK_SECRET': '',
            'FACEBOOK_CLIENT_ID': '',

            'SMARTIDEE_SECRET': '',
            'SMARTIDEE_CLIENT_ID': '',

            'TARA_SECRET': '',
            'TARA_CLIENT_ID': '',
            'TARA_SANDBOX': True,
            'TARA_LABEL': 'Riigi Autentimisteenus',

            'USER_ACTIVATION_URL_TEMPLATE': 'http://example.com/#/activate/{user_uuid}/{token}/',
        }

    @staticmethod
    def get_public_settings():
        return [
            'GOOGLE_CLIENT_ID',
            'FACEBOOK_CLIENT_ID',
            'SMARTIDEE_CLIENT_ID',
            'TARA_CLIENT_ID',
            'TARA_SANDBOX',
            'TARA_LABEL',
        ]

    @staticmethod
    def django_app():
        return 'waldur_auth_social'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns
        return urlpatterns
