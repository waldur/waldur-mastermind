from django.apps import AppConfig


class MarketplaceRancherConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_rancher'
    verbose_name = 'Marketplace Rancher'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import manager

        from . import PLUGIN_NAME, processors

        manager.register(offering_type=PLUGIN_NAME,
                         create_resource_processor=processors.RancherCreateProcessor,
                         delete_resource_processor=processors.RancherDeleteProcessor)
