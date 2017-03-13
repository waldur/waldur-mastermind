from __future__ import unicode_literals

from datetime import timedelta

from nodeconductor.core import NodeConductorExtension


class SupportExtension(NodeConductorExtension):
    class Settings(object):
        WALDUR_SUPPORT = {
            'ACTIVE_BACKEND': 'nodeconductor_assembly_waldur.support.backend.atlassian:JiraBackend',
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
                'summary': '{{issue.summary}}',
                'description': (
                    'Description: {{issue.description}}\n'
                    '{% if issue.project %}'
                    'Project Name: {{issue.project.name}}\n'
                    '{% endif %}'
                    '{% if issue.customer %}'
                    'Organization Name: {{issue.customer.name}}\n'
                    '{% endif %}'
                    '{% if issue.resource %}'
                    'Service Type: {{issue.resource.service_project_link.service.type}}\n'
                    'Affected Resource Name: {{issue.resource}}\n'
                    '{% endif %}'
                ),
            },
            'DEFAULT_OFFERING_ISSUE_TYPE': 'Service Request',
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
                #             'help_text': 'VPC storage limit in GB.',
                #         },
                #         'ram': {
                #             'type': 'integer',
                #             'label': 'Max RAM, GB',
                #             'help_text': 'VPC RAM limit in GB.',
                #         },
                #         'cpu_count': {
                #             'type': 'integer',
                #             'label': 'Max vCPU',
                #             'help_text': 'VPC CPU count limit.',
                #         },
                #     },
                # },
            },
        }

    @staticmethod
    def django_app():
        return 'nodeconductor_assembly_waldur.support'

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
        }
