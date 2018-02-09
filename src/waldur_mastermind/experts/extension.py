from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class ExpertsExtension(WaldurExtension):
    class Settings:
        WALDUR_EXPERTS = {
            'REQUEST_PROJECT_LINK_TEMPLATE': 'https://www.example.com/#/projects/{project_uuid}/experts/{request_uuid}/',
            'REQUEST_CUSTOMER_LINK_TEMPLATE': 'https://www.example.com/#/organizations/{customer_uuid}/experts/{request_uuid}/',
            'INVITATION_LINK_TEMPLATE': 'https://www.example.com/#/invitation/{uuid}/',
            'CURRENCY_NAME': 'EUR',
            'CONTRACT': {
                'offerings': {
                    # 'custom_vpc_experts': {
                    #     'label': 'Custom VPC',
                    #     'order': ['storage', 'ram', 'cpu_count'],
                    #     'category': 'Experts',
                    #     'description': 'Custom VPC example.',
                    #     'summary': '<div>super long long long long long long <b>summary</b></div>',
                    #     'price': 100,
                    #     'recurring_billing': False,  # False if billing is project based, True if monthly occuring.
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
                # 'order': ['objectives', 'milestones', 'terms-and-conditions'],
                # 'options': {
                #     'objectives': {
                #         'order': ['objectives', 'price'],
                #         'label': 'Objectives',
                #         'description': 'Contract objectives.',
                #         'options': {
                #             'objectives': {
                #                 'type': 'text',
                #                 'label': 'Objectives',
                #                 'required': True,
                #                 'default': 'This is an objective.',
                #             },
                #             'price': {
                #                 'type': 'money',
                #                 'label': 'Planned budget',
                #             }
                #         }
                #     },
                #     'milestones': {
                #         'order': ['milestones'],
                #         'label': 'Milestones',
                #         'options': {
                #             'milestones': {
                #                 'type': 'html_text',
                #                 'label': 'Milestones',
                #                 'help_text': 'Defines project milestones.',
                #             }
                #         }
                #     },
                #     'terms-and-conditions': {
                #         'order': ['contract_methodology', 'out_of_scope', 'common_tos'],
                #         'label': 'Terms and conditions',
                #         'options': {
                #             'contract_methodology': {
                #                 'type': 'string',
                #                 'label': 'Contract methodology',
                #             },
                #             'out_of_scope': {
                #                 'type': 'string',
                #                 'label': 'Out of scope',
                #             },
                #             'common_tos': {
                #                 'type': 'text',
                #                 'label': 'Common Terms of Services.',
                #             }
                #         }
                #     },
                # }
            }
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.experts'

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
