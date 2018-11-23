from django.apps import AppConfig
from django.db.models import signals


class MarketplaceSupportConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_support'
    verbose_name = 'Marketplace supports'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace_support import PLUGIN_NAME
        from waldur_mastermind.support import models as support_models

        from . import handlers, processor

        signals.post_save.connect(
            handlers.create_support_template,
            sender=marketplace_models.Offering,
            dispatch_uid='waldur_mastermind.marketpace_support.create_support_template',
        )

        signals.post_save.connect(
            handlers.change_order_item_state,
            sender=support_models.Offering,
            dispatch_uid='waldur_mastermind.marketpace_support.order_item_set_state_done',
        )

        signals.pre_delete.connect(
            handlers.terminate_resource,
            sender=support_models.Offering,
            dispatch_uid='waldur_mastermind.marketpace_support.terminate_resource',
        )

        signals.post_save.connect(
            handlers.create_support_plan,
            sender=marketplace_models.Plan,
            dispatch_uid='waldur_mastermind.marketpace_support.create_support_plan',
        )

        signals.post_save.connect(
            handlers.offering_set_state_ok,
            sender=support_models.Issue,
            dispatch_uid='waldur_mastermind.marketpace_support.offering_set_state_ok',
        )

        manager.register(PLUGIN_NAME,
                         create_resource_processor=processor.CreateResourceProcessor,
                         delete_resource_processor=processor.DeleteResourceProcessor,
                         scope_model=support_models.Offering)
