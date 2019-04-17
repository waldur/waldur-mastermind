from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class MarketplaceSupportExtension(WaldurExtension):
    class Settings:
        WALDUR_MARKETPLACE_SUPPORT = {
            'REQUEST_LINK_TEMPLATE': 'https://www.example.com/#/offering/{request_uuid}/',
            'CREATE_RESOURCE_TEMPLATE': (
                '\n[Order item|{{order_item_url}}].'
                '\nVendor: {{order_item.offering.customer.name}}'
            ),
            'UPDATE_RESOURCE_TEMPLATE': (
                '\n[Switch plan for resource {{order_item.resource.scope.name}}|{{request_url}}].'
                '\nSwitch from {{order_item.resource.plan.name}} plan to {{order_item.plan.name}}.'
            ),
            'TERMINATE_RESOURCE_TEMPLATE': (
                '[Terminate resource {{order_item.resource.scope.name}}|{{request_url}}].'
            ),
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_support'

    @staticmethod
    def is_assembly():
        return True
