from django.apps import AppConfig
from django.db.models import signals

from waldur_mastermind.marketplace.enums import RANCHER_OFFERING
from waldur_mastermind.marketplace_rancher.const import RANCHER_BILLING_COMPONENTS


class MarketplaceRancherConfig(AppConfig):
    name = "waldur_mastermind.marketplace_rancher"
    verbose_name = "Marketplace Rancher"

    def ready(self):
        from waldur_core.structure import signals as structure_signals
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace_rancher import executors
        from waldur_openstack import models as openstack_models
        from waldur_rancher import executors as rancher_executors
        from waldur_rancher import models as rancher_models
        from waldur_rancher.apps import RancherConfig

        from . import handlers, processors

        manager.register(
            offering_type=RANCHER_OFFERING,
            create_resource_processor=processors.RancherCreateProcessor,
            delete_resource_processor=processors.RancherDeleteProcessor,
            service_type=RancherConfig.service_name,
            components=RANCHER_BILLING_COMPONENTS,
            get_importable_resources_backend_method="get_importable_clusters",
            import_resource_backend_method="import_cluster",
            pull_resource_executor=rancher_executors.ClusterPullExecutor,
            import_resource_executor=executors.ManagedRancherImportExecutor,
        )

        marketplace_handlers.connect_resource_metadata_handlers(rancher_models.Cluster)
        marketplace_handlers.connect_resource_handlers(rancher_models.Cluster)

        structure_signals.resource_imported.connect(
            handlers.create_marketplace_resource_for_imported_cluster,
            sender=rancher_models.Cluster,
            dispatch_uid="waldur_mastermind.marketplace_rancher."
            "create_resource_for_imported_cluster",
        )

        signals.post_save.connect(
            handlers.create_offering_user_for_rancher_user,
            sender=rancher_models.RancherUser,
            dispatch_uid="waldur_mastermind.marketplace_rancher.create_offering_user_for_rancher_user",
        )

        signals.pre_delete.connect(
            handlers.drop_offering_user_for_rancher_user,
            sender=rancher_models.RancherUser,
            dispatch_uid="waldur_mastermind.marketplace_rancher.drop_offering_user_for_rancher_user",
        )

        signals.post_save.connect(
            handlers.update_argocd_secret_when_resource_options_changed,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_mastermind.marketplace_rancher.update_argocd_secret_when_resource_options_changed",
        )

        signals.post_save.connect(
            handlers.create_public_cluster_ip_for_floating_ip,
            sender=openstack_models.FloatingIP,
            dispatch_uid="waldur_mastermind.marketplace_rancher.create_public_cluster_ip_for_floating_ip",
        )
