from django.apps import AppConfig
from django.db.models import signals


class MarketplaceSlurmConfig(AppConfig):
    name = "waldur_mastermind.marketplace_slurm_remote"
    verbose_name = "Marketplace SLURM Remote"
    service_name = "SLURM remote"

    def ready(self):
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace import signals as marketplace_signals
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace_slurm_remote import (
            PLUGIN_NAME,
            handlers,
            processor,
        )
        from waldur_mastermind.marketplace_slurm_remote import (
            registrators as slurm_registrators,
        )
        from waldur_slurm import models as slurm_models

        slurm_registrators.SlurmRegistrator.connect()

        signals.post_save.connect(
            handlers.update_component_quota,
            sender=slurm_models.Allocation,
            dispatch_uid="waldur_mastermind.marketplace_slurm.update_component_quota",
        )

        marketplace_handlers.connect_resource_handlers(slurm_models.Allocation)
        marketplace_handlers.connect_resource_metadata_handlers(slurm_models.Allocation)

        manager.register(
            PLUGIN_NAME,
            create_resource_processor=processor.CreateAllocationProcessor,
            update_resource_processor=processor.UpdateAllocationLimitsProcessor,
            delete_resource_processor=processor.DeleteAllocationProcessor,
        )

        marketplace_signals.resource_deletion_succeeded.connect(
            handlers.terminate_allocation_when_resource_is_terminated,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_mastermind.marketplace_slurm_remote.terminate_allocation_when_resource_is_terminated",
        )
