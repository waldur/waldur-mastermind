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
            },
            'OFFERING': {
                'transformation': {
                    'summary': {},
                    'description': {},
                    'type': {
                        'default': 'Service Request',
                        'help_text': '',
                    },
                    'status': {
                        'type': 'integer',
                    },
                    'project': {
                        'type': 'hyperlinked', # handled as HyperlinkedRelatedField.
                    }
                },
                'devops': {
                    'summary': {},
                    'description': {},
                    'type': {
                        'default': 'Service Request',
                        'help_text': '',
                    },
                    'status': {
                        'type': 'integer',
                    }
                },
                'recovery': {
                    'summary': {},
                    'description': {},
                    'type': {
                        'default': 'Service Request',
                        'help_text': '',
                    },
                    'status': {
                        'type': 'integer',
                    }
                },
                'managed_apps': {
                    'summary': {},
                    'description': {},
                    'type': {
                        'default': 'Service Request',
                        'help_text': '',
                    },
                    'status': {
                        'type': 'integer',
                    }
                },
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
