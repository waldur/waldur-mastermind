from datetime import timedelta

from waldur_core.core import WaldurExtension


class SupportExtension(WaldurExtension):
    class Settings:
        WALDUR_SUPPORT = {
            # wiki for global options: https://opennode.atlassian.net/wiki/display/WD/Assembly+plugin+configuration
            'ENABLED': False,
            # wiki for JIRA-specific options:
            # https://opennode.atlassian.net/wiki/display/WD/JIRA+Service+Desk+configuration
            'ACTIVE_BACKEND': 'waldur_mastermind.support.backend.atlassian:ServiceDeskBackend',
            'USE_OLD_API': False,
            'USE_TEENAGE_API': False,
            'USE_AUTOMATIC_REQUEST_MAPPING': True,
            'MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS': False,
            'STRANGE_SETTING': 1,
            'CREDENTIALS': {
                'server': 'http://example.com/',
                'username': 'USERNAME',
                'password': 'PASSWORD',
                'email': '',
                'token': '',
                'verify_ssl': False,
            },
            'PROJECT': {'key': 'PROJECT',},
            'ISSUE': {
                'types': [
                    'Informational',
                    'Service Request',
                    'Change Request',
                    'Incident',
                ],
                'impact_field': 'Impact',
                'reporter_field': 'Original Reporter',
                'caller_field': 'Caller',
                'sla_field': 'Time to first response',
                'type_of_linked_issue': 'Relates',
                'satisfaction_field': 'Customer satisfaction',
                'request_feedback': 'Request feedback',  # a field of checkbox type and with a single option 'yes'.
            },
            'DEFAULT_OFFERING_ISSUE_TYPE': 'Service Request',
            'EXCLUDED_ATTACHMENT_TYPES': [],
        }

        SUPPRESS_NOTIFICATION_EMAILS = False
        ISSUE_FEEDBACK_ENABLE = False
        # Measured in days
        ISSUE_FEEDBACK_TOKEN_PERIOD = 7

    @staticmethod
    def django_app():
        return 'waldur_mastermind.support'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def celery_tasks():
        return {
            'pull-support-users': {
                'task': 'waldur_mastermind.support.pull_support_users',
                'schedule': timedelta(hours=6),
                'args': (),
            },
            'pull-priorities': {
                'task': 'waldur_mastermind.support.pull_priorities',
                'schedule': timedelta(hours=24),
                'args': (),
            },
        }
