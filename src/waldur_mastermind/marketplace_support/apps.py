from django.apps import AppConfig
from django.db.models import signals


class MarketplaceSupportConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_support'
    verbose_name = 'Marketplace supports'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
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
            dispatch_uid='waldur_mastermind.marketpace_support.change_order_item_state',
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
            handlers.change_offering_state,
            sender=support_models.Issue,
            dispatch_uid='waldur_mastermind.marketpace_support.change_offering_state',
        )

        signals.post_save.connect(
            handlers.update_order_item_if_issue_was_complete,
            sender=support_models.Issue,
            dispatch_uid='waldur_mastermind.marketpace_support.update_order_item_if_issue_was_complete',
        )

        manager.register(PLUGIN_NAME,
                         create_resource_processor=processor.CreateRequestProcessor,
                         update_resource_processor=processor.UpdateRequestProcessor,
                         delete_resource_processor=processor.DeleteRequestProcessor,
                         can_terminate_order_item=True)

        marketplace_handlers.connect_resource_metadata_handlers(support_models.Offering)
