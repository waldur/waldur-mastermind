from django.apps import AppConfig


class MarketplaceWaldurConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_waldur'
    verbose_name = 'Marketplace Waldur'

    def ready(self):
        from waldur_waldur.apps import RemoteWaldurConfig
        from waldur_mastermind.marketplace.plugins import manager
        from . import PLUGIN_NAME

        manager.register(
            offering_type=PLUGIN_NAME, service_name=RemoteWaldurConfig.service_name
        )
