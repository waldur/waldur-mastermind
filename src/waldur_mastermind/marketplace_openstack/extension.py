from django.core.exceptions import ObjectDoesNotExist

from waldur_core.core import WaldurExtension


class MarketplaceOpenStackExtension(WaldurExtension):
    class Settings:
        WALDUR_MARKETPLACE_OPENSTACK = {
            'AUTOMATICALLY_CREATE_PRIVATE_OFFERING': True,
        }

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_openstack'

    @staticmethod
    def get_dynamic_settings():
        from waldur_mastermind.marketplace.models import Category

        settings = {
            item[0]: item[1]
            for item in [
                ('INSTANCE_CATEGORY_UUID', 'vm'),
                ('VOLUME_CATEGORY_UUID', 'volume'),
                ('TENANT_CATEGORY_UUID', 'tenant'),
            ]
        }

        for key, name in settings.items():
            try:
                predicate = {'default_%s_category' % name: True}
                category_uuid = Category.objects.get(**predicate).uuid
            except ObjectDoesNotExist:
                settings[key] = None
            else:
                settings[key] = category_uuid

        return settings

    @staticmethod
    def is_assembly():
        return True
