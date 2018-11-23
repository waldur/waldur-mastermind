from django.apps import AppConfig
from django.db.models import signals


class MarketplaceSlurmConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_slurm'
    verbose_name = 'Marketplace SLURM'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import Component, manager
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
        from waldur_slurm import models as slurm_models
        from waldur_core.structure import models as structure_models

        from . import handlers, processor

        signals.post_save.connect(
            handlers.create_slurm_package,
            sender=marketplace_models.PlanComponent,
            dispatch_uid='waldur_mastermind.marketpace_slurm.create_slurm_package',
        )

        signals.post_save.connect(
            handlers.create_slurm_usage,
            sender=slurm_models.AllocationUsage,
            dispatch_uid='waldur_mastermind.marketpace_slurm.create_slurm_usage',
        )

        signals.post_save.connect(
            handlers.update_component_quota,
            sender=slurm_models.Allocation,
            dispatch_uid='waldur_mastermind.marketpace_slurm.update_component_quota',
        )

        signals.post_save.connect(
            handlers.change_order_item_state,
            sender=slurm_models.Allocation,
            dispatch_uid='waldur_mastermind.marketpace_slurm.change_order_item_state',
        )

        signals.pre_delete.connect(
            handlers.terminate_resource,
            sender=slurm_models.Allocation,
            dispatch_uid='waldur_mastermind.marketpace_slurm.terminate_resource',
        )

        USAGE = marketplace_models.OfferingComponent.BillingTypes.USAGE
        manager.register(PLUGIN_NAME,
                         create_resource_processor=processor.CreateResourceProcessor,
                         delete_resource_processor=processor.DeleteResourceProcessor,
                         scope_model=structure_models.ServiceSettings,
                         components=(
                             Component(type='cpu', name='CPU', measured_unit='hours', billing_type=USAGE),
                             Component(type='gpu', name='GPU', measured_unit='hours', billing_type=USAGE),
                             Component(type='ram', name='RAM', measured_unit='GB', billing_type=USAGE),
                         ))
