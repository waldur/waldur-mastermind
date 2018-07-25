from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class AuthSocialExtension(WaldurExtension):

    class Settings:
        # wiki: https://opennode.atlassian.net/wiki/display/WD/AuthSocial+plugin+configuration
        WALDUR_AUTH_SOCIAL = {
            'GOOGLE_SECRET': 'PLACEHOLDER',
            'GOOGLE_CLIENT_ID': 'PLACEHOLDER',

            'FACEBOOK_SECRET': 'PLACEHOLDER',
            'FACEBOOK_CLIENT_ID': 'PLACEHOLDER',

            'SMARTIDEE_SECRET': 'PLACEHOLDER',
            'SMARTIDEE_CLIENT_ID': 'PLACEHOLDER',

            'USER_ACTIVATION_URL_TEMPLATE': 'http://example.com/#/activate/{user_uuid}/{token}/',
        }

    @staticmethod
    def get_public_settings():
        return ['GOOGLE_CLIENT_ID', 'FACEBOOK_CLIENT_ID', 'SMARTIDEE_CLIENT_ID']

    @staticmethod
    def django_app():
        return 'waldur_auth_social'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns
        return urlpatterns
