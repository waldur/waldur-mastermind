from django.apps import AppConfig


class MarketplaceRemoteConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_remote'
    verbose_name = 'Remote Marketplace'

    def ready(self):
        from waldur_mastermind.marketplace import plugins
        from waldur_mastermind.marketplace_remote import PLUGIN_NAME
        from waldur_mastermind.marketplace_remote import processors

        plugins.manager.register(
            offering_type=PLUGIN_NAME,
            create_resource_processor=processors.RemoteCreateResourceProcessor,
            update_resource_processor=processors.RemoteUpdateResourceProcessor,
            delete_resource_processor=processors.RemoteDeleteResourceProcessor,
        )
