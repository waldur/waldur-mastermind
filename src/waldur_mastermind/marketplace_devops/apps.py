from django.apps import AppConfig


class MarketplaceDevOpsConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_devops'
    verbose_name = 'Marketplace DevOps'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import manager

        from . import PLUGIN_NAME, processors

        manager.register(
            offering_type=PLUGIN_NAME,
            create_resource_processor=processors.CreateProcessor,
            update_resource_processor=processors.UpdateProcessor,
            delete_resource_processor=processors.DeleteProcessor,
        )
