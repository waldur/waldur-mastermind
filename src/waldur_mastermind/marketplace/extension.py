from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class MarketplaceExtension(WaldurExtension):
    class Settings:
        WALDUR_MARKETPLACE = {
            'THUMBNAIL_SIZE': (120, 120),
            'THUMBNAIL_SUFFIX': 'thumbnail',
            'OWNER_CAN_APPROVE_ORDER': True,
            'MANAGER_CAN_APPROVE_ORDER': False,
            'ADMIN_CAN_APPROVE_ORDER': False,
            'NOTIFY_STAFF_ABOUT_APPROVALS': False,
            'OWNER_CAN_REGISTER_SERVICE_PROVIDER': False,
            'ORDER_LINK_TEMPLATE': 'https://www.example.com/#/projects/'
                                   '{project_uuid}/marketplace-order-list/',
            'ORDER_ITEM_LINK_TEMPLATE': 'https://www.example.com/#/projects/{project_uuid}/'
                                        'marketplace-order-item-details/{order_item_uuid}/'
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def get_public_settings():
        return [
            'OWNER_CAN_APPROVE_ORDER',
            'MANAGER_CAN_APPROVE_ORDER',
            'ADMIN_CAN_APPROVE_ORDER',
            'OWNER_CAN_REGISTER_SERVICE_PROVIDER',
        ]

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
        return {}
