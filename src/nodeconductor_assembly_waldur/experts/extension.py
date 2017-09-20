from __future__ import unicode_literals

from nodeconductor.core import NodeConductorExtension


class ExpertsExtension(NodeConductorExtension):
    class Settings:
        WALDUR_EXPERTS = {
            'REQUEST_LINK_TEMPLATE': 'https://www.example.com/#/experts/{uuid}/',
            'CONTRACT': {
                'order': ['objectives', 'milestones', 'terms-and-conditions'],
                'options': {
                    'objectives': {
                        'order': ['objectives', 'price'],
                        'label': 'Objectives',
                        'description': 'Contract objectives.',
                        'options': {
                            'objectives': {
                                'type': 'string',
                                'label': 'Objectives',
                                'required': True,
                                'default': 'This is an objective.',
                            },
                            'price': {
                                'type': 'integer',
                                'label': 'Planned budget',
                            }
                        }
                    },
                    'milestones': {
                        'order': ['milestones'],
                        'label': 'Milestones',
                        'options': {
                            'milestones': {
                                'type': 'html_text',
                                'label': 'Milestones',
                                'help_text': 'Defines project milestones.',
                            }
                        }
                    },
                    'terms-and-conditions': {
                        'order': ['contract_methodology', 'out_of_scope', 'common_tos'],
                        'label': 'Terms and conditions',
                        'options': {
                            'contract_methodology': {
                                'type': 'string',
                                'label': 'Contract methodology',
                            },
                            'out_of_scope': {
                                'type': 'string',
                                'label': 'Out of scope',
                            },
                            'common_tos': {
                                'type': 'string',
                                'label': 'Common Terms of Services.',
                            }
                        }
                    },
                }
            }
        }

    @staticmethod
    def django_app():
        return 'nodeconductor_assembly_waldur.experts'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def celery_tasks():
        return {}
