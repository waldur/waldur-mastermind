from django.apps import AppConfig


class MarketplaceRancherConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_rancher'
    verbose_name = 'Marketplace Rancher'

    def ready(self):
        from waldur_core.structure import signals as structure_signals
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import manager, Component
        from waldur_rancher.apps import RancherConfig
        from waldur_rancher import models as rancher_models

        from . import handlers, PLUGIN_NAME, processors

        USAGE = marketplace_models.OfferingComponent.BillingTypes.USAGE
        manager.register(offering_type=PLUGIN_NAME,
                         create_resource_processor=processors.RancherCreateProcessor,
                         delete_resource_processor=processors.RancherDeleteProcessor,
                         components=(
                             Component(type='node', name='K8S node', measured_unit='', billing_type=USAGE),
                         ),
                         service_type=RancherConfig.service_name,
                         resource_model=rancher_models.Cluster)

        marketplace_handlers.connect_resource_metadata_handlers(rancher_models.Cluster)
        marketplace_handlers.connect_resource_handlers(rancher_models.Cluster)

        structure_signals.resource_imported.connect(
            handlers.create_marketplace_resource_for_imported_cluster,
            sender=rancher_models.Cluster,
            dispatch_uid='waldur_mastermind.marketpace_rancher.'
                         'create_resource_for_imported_cluster',
        )
