from waldur_core.core import WaldurExtension


class AuthSocialExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_auth_social'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            'waldur-pull-remote-eduteams-users': {
                'task': 'waldur_auth_social.pull_remote_eduteams_users',
                'schedule': timedelta(minutes=5),
                'args': (),
            },
        }
