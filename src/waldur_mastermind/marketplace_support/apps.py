from django.apps import AppConfig
from django.db.models import signals


class MarketplaceSupportConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_support'
    verbose_name = 'Marketplace supports'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace_support import PLUGIN_NAME
        from waldur_mastermind.support import models as support_models
        from waldur_core.core import signals as core_signals
        from waldur_mastermind.marketplace import serializers as marketplace_serializers
        from waldur_mastermind.marketplace_support.serializers import add_issue

        from . import handlers, processor, registrators

        registrators.SupportRegistrator.connect()

        signals.post_save.connect(
            handlers.update_order_item_if_issue_was_complete,
            sender=support_models.Issue,
            dispatch_uid='waldur_mastermind.marketplace_support.update_order_item_if_issue_was_complete',
        )

        signals.post_save.connect(
            handlers.notify_about_request_based_item_creation,
            sender=support_models.Issue,
            dispatch_uid='waldur_mastermind.marketplace_support.notify_about_request_based_item_creation',
        )

        manager.register(
            PLUGIN_NAME,
            create_resource_processor=processor.CreateRequestProcessor,
            update_resource_processor=processor.UpdateRequestProcessor,
            delete_resource_processor=processor.DeleteRequestProcessor,
            can_terminate_order_item=True,
            enable_usage_notifications=True,
            enable_remote_support=True,
            can_update_limits=True,
        )

        core_signals.pre_serializer_fields.connect(
            sender=marketplace_serializers.OrderItemDetailsSerializer,
            receiver=add_issue,
        )
