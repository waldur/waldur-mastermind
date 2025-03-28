from django.apps import AppConfig
from django.db.models import signals


class MarketplaceSlurmConfig(AppConfig):
    name = "waldur_mastermind.marketplace_slurm_remote"
    verbose_name = "Marketplace SLURM Remote"
    service_name = "SLURM remote"

    def ready(self):
        from waldur_core.permissions import signals as permission_signals
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace_slurm_remote import (
            PLUGIN_NAME,
            handlers,
            processor,
        )
        from waldur_mastermind.marketplace_slurm_remote import (
            registrators as slurm_registrators,
        )

        slurm_registrators.RemoteSlurmRegistrator.connect()

        manager.register(
            PLUGIN_NAME,
            create_resource_processor=processor.CreateAllocationProcessor,
            update_resource_processor=processor.UpdateAllocationLimitsProcessor,
            delete_resource_processor=processor.DeleteAllocationProcessor,
            can_update_limits=True,
            enable_remote_support=True,
        )

        signals.post_save.connect(
            handlers.send_pending_order_to_message_queue,
            sender=marketplace_models.Order,
            dispatch_uid="waldur_mastermind.marketplace_slurm_remote.send_pending_order_to_message_queue",
        )

        signals.post_save.connect(
            handlers.send_resource_update_message_to_mqtt,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_mastermind.marketplace_slurm_remote.send_resource_status_changed_message_to_mqtt",
        )

        permission_signals.role_granted.connect(
            handlers.send_role_granted_message_to_mqtt,
            dispatch_uid="waldur_mastermind.marketplace_slurm_remote.send_role_granted_message_to_mqtt",
        )

        permission_signals.role_revoked.connect(
            handlers.send_role_revoked_message_to_mqtt,
            dispatch_uid="waldur_mastermind.marketplace_slurm_remote.send_role_revoked_message_to_mqtt",
        )
