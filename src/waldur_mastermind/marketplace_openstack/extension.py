from datetime import timedelta

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
            "marketplace-openstack.terminate-child-resources-of-terminated-tenants": {
                "task": "waldur_mastermind.marketplace_openstack.terminate_child_resources_of_terminated_tenants",
                "schedule": timedelta(hours=24),
                "args": (),
            },
        }

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in
