from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist

from waldur_core.core import WaldurExtension


class MarketplaceOpenStackExtension(WaldurExtension):
    class Settings:
        WALDUR_MARKETPLACE_OPENSTACK = {
            "AUTOMATICALLY_CREATE_PRIVATE_OFFERING": True,
        }

    @staticmethod
    def django_app():
        return "waldur_mastermind.marketplace_openstack"

    @staticmethod
    def get_dynamic_settings():
        from waldur_mastermind.marketplace.models import Category

        settings = {
            item[0]: item[1]
            for item in [
                ("INSTANCE_CATEGORY_UUID", "vm"),
                ("VOLUME_CATEGORY_UUID", "volume"),
                ("TENANT_CATEGORY_UUID", "tenant"),
            ]
        }

        for key, name in settings.items():
            try:
                predicate = {"default_%s_category" % name: True}
                category_uuid = Category.objects.get(**predicate).uuid
            except ObjectDoesNotExist:
                settings[key] = None
            else:
                settings[key] = category_uuid

        return settings

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def celery_tasks():
        return {
            "marketplace-openstack.create-resources-for-lost-instances-and-volumes": {
                "task": "waldur_mastermind.marketplace_openstack.create_resources_for_lost_instances_and_volumes",
                "schedule": timedelta(hours=6),
                "args": (),
            },
            "marketplace-openstack.refresh-instance-backend-metadata": {
                "task": "waldur_mastermind.marketplace_openstack.refresh_instance_backend_metadata",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "mark_terminating_resources_as_erred_after_timeout": {
                "task": "waldur_mastermind.marketplace_openstack.mark_terminating_resources_as_erred_after_timeout",
                "schedule": timedelta(hours=2),
                "args": (),
            },
        }
