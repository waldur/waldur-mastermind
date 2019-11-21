from django.apps import AppConfig


class MarketplaceRancherConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_rancher'
    verbose_name = 'Marketplace Rancher'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import manager, Component
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_rancher.apps import RancherConfig

        from . import PLUGIN_NAME, processors

        USAGE = marketplace_models.OfferingComponent.BillingTypes.USAGE
        manager.register(offering_type=PLUGIN_NAME,
                         create_resource_processor=processors.RancherCreateProcessor,
                         delete_resource_processor=processors.RancherDeleteProcessor,
                         components=(
                             Component(type='node', name='K8S node', measured_unit='', billing_type=USAGE),
                         ),
                         service_type=RancherConfig.service_name)
