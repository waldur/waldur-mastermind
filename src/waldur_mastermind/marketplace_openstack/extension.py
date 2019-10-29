from waldur_core.core import WaldurExtension


class MarketplaceOpenStackExtension(WaldurExtension):

    class Settings:
        WALDUR_MARKETPLACE_OPENSTACK = {
            'TENANT_CATEGORY_UUID': None,
            'INSTANCE_CATEGORY_UUID': None,
            'VOLUME_CATEGORY_UUID': None,
            'AUTOMATICALLY_CREATE_PRIVATE_OFFERING': True,
            'BILLING_ENABLED': True,
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_openstack'

    @staticmethod
    def get_public_settings():
        return [
            'TENANT_CATEGORY_UUID',
            'INSTANCE_CATEGORY_UUID',
            'VOLUME_CATEGORY_UUID',
        ]

    @staticmethod
    def is_assembly():
        return True
