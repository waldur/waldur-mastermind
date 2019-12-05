from datetime import timedelta

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
            'NOTIFY_ABOUT_RESOURCE_CHANGE': True,
            'DISABLE_SENDING_NOTIFICATIONS_ABOUT_RESOURCE_UPDATE': True,
            'OWNER_CAN_REGISTER_SERVICE_PROVIDER': False,
            'ORDER_LINK_TEMPLATE': 'https://www.example.com/#/projects/'
                                   '{project_uuid}/marketplace-order-list/',
            'ORDER_ITEM_LINK_TEMPLATE': 'https://www.example.com/#/projects/{project_uuid}/'
                                        'marketplace-order-item-details/{order_item_uuid}/',
            'PUBLIC_RESOURCES_LINK_TEMPLATE': 'https://www.example.com/#/organizations/{organization_uuid}/'
                                        'marketplace-public-resources/',
            'PLAN_TEMPLATE': 'Plan: {{ plan.name }}'
                             '{% for component in components %}\n'
                             '{{component.name}}; '
                             'amount: {{component.amount}}; '
                             'price: {{component.price|floatformat }};'
                             '{% endfor %}'
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
        from celery.schedules import crontab
        return {
            'waldur-marketplace-calculate-usage': {
                'task': 'waldur_mastermind.marketplace.calculate_usage_for_current_month',
                'schedule': timedelta(hours=1),
                'args': (),
            },
            'waldur-mastermind-send-notifications-about-usages': {
                'task': 'waldur_mastermind.marketplace.send_notifications_about_usages',
                'schedule': crontab(minute=0, hour=15, day_of_month='23'),
                'args': (),
            }
        }
