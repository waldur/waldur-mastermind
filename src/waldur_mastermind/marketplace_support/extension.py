from waldur_core.core import WaldurExtension


class MarketplaceSupportExtension(WaldurExtension):
    class Settings:
        WALDUR_MARKETPLACE_SUPPORT = {
            'REQUEST_LINK_TEMPLATE': 'https://www.example.com/#/offering/{request_uuid}/',
            'CREATE_RESOURCE_TEMPLATE': (
                '{% load waldur_marketplace %}'
                '\n[Order item|{{order_item_url}}].'
                '\nProvider: {{order_item.offering.customer.name}}'
                '\nPlan details:\n{% plan_details order_item.plan %}'
            ),
            'UPDATE_RESOURCE_TEMPLATE': (
                '\n[Switch plan for resource {{order_item.resource.scope.name}}|{{request_url}}].'
                '\nSwitch from {{order_item.resource.plan.name}} plan to {{order_item.plan.name}}.'
                '\nMarketplace resource UUID: {{order_item.resource.uuid.hex}}'
            ),
            'TERMINATE_RESOURCE_TEMPLATE': (
                '{% load waldur_marketplace %}'
                '[Terminate resource {{order_item.resource.scope.name}}|{{request_url}}].'
                '\n{% plan_details order_item.resource.plan %}'
                '\nMarketplace resource UUID: {{order_item.resource.uuid.hex}}'
            ),
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_support'

    @staticmethod
    def is_assembly():
        return True
