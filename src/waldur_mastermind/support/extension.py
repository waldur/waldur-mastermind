from __future__ import unicode_literals

from datetime import timedelta

from waldur_core.core import WaldurExtension


class SupportExtension(WaldurExtension):
    class Settings(object):
        WALDUR_SUPPORT = {
            # wiki for global options: https://opennode.atlassian.net/wiki/display/WD/Assembly+plugin+configuration
            'ENABLED': False,
            # wiki for JIRA-specific options:
            # https://opennode.atlassian.net/wiki/display/WD/JIRA+Service+Desk+configuration
            'ACTIVE_BACKEND': 'waldur_mastermind.support.backend.atlassian:ServiceDeskBackend',
            'USE_OLD_API': False,
            'CREDENTIALS': {
                'server': 'http://example.com/',
                'username': 'USERNAME',
                'password': 'PASSWORD',
                'verify_ssl': False,
            },
            'PROJECT': {
                'key': 'PROJECT',
            },
            'ISSUE': {
                'types': ['Informational', 'Service Request', 'Change Request', 'Incident'],
                'impact_field': 'Impact',
                'reporter_field': 'Original Reporter',
                'caller_field': 'Caller',
                'sla_field': 'Time to first response',
                # 'organisation_field': 'Reporter organization',
                # 'project_field': 'Waldur project',
                # 'affected_resource_field': 'Affected resource',
                # 'template_field': 'Waldur template',
                'summary': '{{issue.summary}}',
                'description': (
                    '{{issue.description}}\n\n'
                    '---\n'
                    'Additional Info: \n'
                    '{% if issue.customer %}'
                    '- Organization: {{issue.customer.name}}\n'
                    '{% endif %}'
                    '{% if issue.project %}'
                    '- Project: {{issue.project.name}}\n'
                    '{% endif %}'
                    '{% if issue.resource %}'
                    '{% if issue.resource.service_project_link and issue.resource.service_project_link.service %}'
                    '{% if issue.resource.service_project_link.service.type %}'
                    '- Service type: {{issue.resource.service_project_link.service.type}}\n'
                    '{% endif %}'
                    '{% endif %}'
                    '- Affected resource: {{issue.resource}}\n'
                    '{% endif %}'
                ),
            },
            'DEFAULT_OFFERING_ISSUE_TYPE': 'Service Request',
            'TERMINATED_OFFERING_LIFETIME': timedelta(weeks=2),
            'OFFERINGS': {
                # An example of configuration for debugging purposes.
                # Add it to settings file to enable Custom VPC offering
                # 'custom_vpc': {
                #     'label': 'Custom VPC',
                #     'order': ['storage', 'ram', 'cpu_count'],
                #     'icon': 'fa-gear',
                #     'category': 'Custom requests',
                #     'description': 'Custom VPC example.',
                #     'options': {
                #         'storage': {
                #             'type': 'integer',
                #             'label': 'Max storage, GB',
                #             'required': True,
                #             'help_text': 'VPC storage limit in GB.',
                #         },
                #         'ram': {
                #             'type': 'integer',
                #             'label': 'Max RAM, GB',
                #             'required': True,
                #             'help_text': 'VPC RAM limit in GB.',
                #         },
                #         'cpu_count': {
                #             'type': 'integer',
                #             'label': 'Max vCPU',
                #             'required': True,
                #             'help_text': 'VPC CPU count limit.',
                #         },
                #     },
                # },
            },
            'EXCLUDED_ATTACHMENT_TYPES': [],
        }

        SUPPRESS_NOTIFICATION_EMAILS = False
        ISSUE_LINK_TEMPLATE = 'https://www.example.com/#/support/issue/{uuid}/'

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
                'task': 'support.SupportUserPullTask',
                'schedule': timedelta(hours=6),
                'args': (),
            },
            'remove-terminated-offerings': {
                'task': 'waldur_mastermind.support.remove_terminated_offerings',
                'schedule': timedelta(hours=24),
                'args': (),
            },
        }

    @staticmethod
    def get_cleanup_executor():
        from waldur_mastermind.support.executors import SupportCleanupExecutor
        return SupportCleanupExecutor
