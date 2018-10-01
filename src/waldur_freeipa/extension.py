from waldur_core.core import WaldurExtension


class FreeIPAExtension(WaldurExtension):
    class Settings:
        WALDUR_FREEIPA = {
            'ENABLED': False,
            'HOSTNAME': 'ipa.example.com',
            'USERNAME': 'admin',
            'PASSWORD': 'secret',
            'VERIFY_SSL': True,
            'USERNAME_PREFIX': 'waldur_',
            'GROUPNAME_PREFIX': 'waldur_',
            'BLACKLISTED_USERNAMES': ['root'],
        }

    @staticmethod
    def django_app():
        return 'waldur_freeipa'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def get_public_settings():
        return ['USERNAME_PREFIX']

    @staticmethod
    def celery_tasks():
        from datetime import timedelta
        return {
            'waldur-freeipa-sync-groups': {
                'task': 'waldur_freeipa.sync_groups',
                'schedule': timedelta(minutes=10),
                'args': (),
            },
        }
