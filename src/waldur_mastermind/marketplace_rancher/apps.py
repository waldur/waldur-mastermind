from django.apps import AppConfig
from django.db.models import signals


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

        from . import handlers, PLUGIN_NAME, processors, registrators

        registrators.RancherRegistrator.connect()

        USAGE = marketplace_models.OfferingComponent.BillingTypes.USAGE
        manager.register(
            offering_type=PLUGIN_NAME,
            create_resource_processor=processors.RancherCreateProcessor,
            delete_resource_processor=processors.RancherDeleteProcessor,
            components=(
                Component(
                    type='node',
                    name='K8S node',
                    measured_unit='nodes',
                    billing_type=USAGE,
                ),
            ),
            service_type=RancherConfig.service_name,
            get_importable_resources_backend_method='get_importable_clusters',
            import_resource_backend_method='import_cluster',
        )

        marketplace_handlers.connect_resource_metadata_handlers(rancher_models.Cluster)
        marketplace_handlers.connect_resource_handlers(rancher_models.Cluster)

        structure_signals.resource_imported.connect(
            handlers.create_marketplace_resource_for_imported_cluster,
            sender=rancher_models.Cluster,
            dispatch_uid='waldur_mastermind.marketplace_rancher.'
            'create_resource_for_imported_cluster',
        )

        signals.post_save.connect(
            handlers.update_node_usage,
            sender=rancher_models.Node,
            dispatch_uid='waldur_mastermind.marketplace_rancher.update_node_usage',
        )

        signals.post_save.connect(
            handlers.create_offering_user_for_rancher_user,
            sender=rancher_models.RancherUser,
            dispatch_uid='waldur_mastermind.marketplace_rancher.create_offering_user_for_rancher_user',
        )

        signals.pre_delete.connect(
            handlers.drop_offering_user_for_rancher_user,
            sender=rancher_models.RancherUser,
            dispatch_uid='waldur_mastermind.marketplace_rancher.drop_offering_user_for_rancher_user',
        )
