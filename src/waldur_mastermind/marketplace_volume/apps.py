from django.apps import AppConfig
from django.db.models import signals


class MarketplaceVolumeConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_volume'
    verbose_name = 'Marketplace OpenStack Volume'

    def ready(self):
        from waldur_core.structure import models as structure_models
        from waldur_openstack.openstack import models as openstack_models
        from waldur_openstack.openstack_tenant import models as tenant_models
        from waldur_mastermind.marketplace.plugins import manager

        from . import handlers, processor, PLUGIN_NAME

        signals.post_save.connect(
            handlers.create_offering_from_tenant,
            sender=openstack_models.Tenant,
            dispatch_uid='waldur_mastermind.marketpace_volume.create_offering_from_tenant',
        )

        signals.pre_delete.connect(
            handlers.archive_offering,
            sender=structure_models.ServiceSettings,
            dispatch_uid='waldur_mastermind.marketpace_volume.archive_offering',
        )

        signals.post_save.connect(
            handlers.change_order_item_state,
            sender=tenant_models.Volume,
            dispatch_uid='waldur_mastermind.marketpace_volume.change_order_item_state',
        )

        manager.register(offering_type=PLUGIN_NAME,
                         processor=processor.OrderItemProcessor,
                         scope_model=structure_models.ServiceSettings)
