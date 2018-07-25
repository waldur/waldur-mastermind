from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class JiraExtension(WaldurExtension):

    class Settings:
        WALDUR_JIRA = {
            'COMMENT_TEMPLATE': '{body}\n\n_(added by {user.full_name} [{user.username}] via G-Cloud Portal)_',
            'ISSUE_TEMPLATE': {
                'RESOURCE_INFO': '\nAffected resource: {resource}\n'
            },
            'ISSUE': {
                'resolution_sla_field': 'Time to resolution',
            },
            'ISSUE_IMPORT_LIMIT': 10
        }

    @staticmethod
    def django_app():
        return 'waldur_jira'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns
        return urlpatterns

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    # @staticmethod
    # def celery_tasks():
    #     return {
    #         'waldur-import-jira-projects': {
    #             'task': 'waldur_jira.ImportProjects',
    #             'schedule': timedelta(minutes=1),
    #             'args': (),
    #         },
    #     }
