from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class MarketplaceOpenStackExtension(WaldurExtension):

    class Settings:
        WALDUR_MARKETPLACE_OPENSTACK = {
            'TENANT_CATEGORY_UUID': None,
            'INSTANCE_CATEGORY_UUID': None,
            'VOLUME_CATEGORY_UUID': None,
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_openstack'

    @staticmethod
    def is_assembly():
        return True
