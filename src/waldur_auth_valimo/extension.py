from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class AuthValimoExtension(WaldurExtension):

    class Settings:
        WALDUR_AUTH_VALIMO = {
            'URL': None,
            'AP_ID': None,
            'AP_PWD': None,
            'DNSName': '',
            'SignatureProfile': None,
            'cert_path': None,
            'key_path': None,
            'message_prefix': 'Login code:',
            'verify_ssl': False,
            'LABEL': 'Mobile ID',
            'MOBILE_PREFIX': '+372',
        }

    @staticmethod
    def get_public_settings():
        return ['LABEL', 'MOBILE_PREFIX']

    @staticmethod
    def django_app():
        return 'waldur_auth_valimo'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def celery_tasks():
        from datetime import timedelta
        return {
            'valimo-auth-cleanup-auth-results': {
                'task': 'waldur_auth_valimo.cleanup_auth_results',
                'schedule': timedelta(hours=1),
            },
        }
