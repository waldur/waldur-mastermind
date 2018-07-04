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
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace'

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
