from django.apps import AppConfig
from django.db.models import signals


class MarketplaceConfig(AppConfig):
    name = 'waldur_mastermind.marketplace'
    verbose_name = 'Marketplace'

    def ready(self):
        from waldur_core.core import signals as core_signals
        from waldur_core.quotas import signals as quota_signals
        from waldur_core.structure import SupportedServices

        from . import handlers, models, utils, signals as marketplace_signals

        signals.post_save.connect(
            handlers.create_screenshot_thumbnail,
            sender=models.Screenshot,
            dispatch_uid='waldur_mastermind.marketplace.create_screenshot_thumbnail',
        )

        signals.post_save.connect(
            handlers.log_order_events,
            sender=models.Order,
            dispatch_uid='waldur_mastermind.marketplace.log_order_events',
        )

        signals.post_save.connect(
            handlers.log_order_item_events,
            sender=models.OrderItem,
            dispatch_uid='waldur_mastermind.marketplace.log_order_item_events',
        )

        signals.post_save.connect(
            handlers.log_resource_events,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.log_resource_events',
        )

        signals.post_save.connect(
            handlers.reject_order,
            sender=models.Order,
            dispatch_uid='waldur_mastermind.marketplace.reject_order',
        )

        signals.post_save.connect(
            handlers.complete_order_when_all_items_are_done,
            sender=models.OrderItem,
            dispatch_uid='waldur_mastermind.marketplace.complete_order_when_all_items_are_done',
        )

        signals.post_save.connect(
            handlers.update_category_quota_when_offering_is_created,
            sender=models.Offering,
            dispatch_uid='waldur_mastermind.marketplace.update_category_quota_when_offering_is_created',
        )

        signals.post_delete.connect(
            handlers.update_category_quota_when_offering_is_deleted,
            sender=models.Offering,
            dispatch_uid='waldur_mastermind.marketplace.update_category_quota_when_offering_is_deleted',
        )

        quota_signals.recalculate_quotas.connect(
            handlers.update_category_offerings_count,
            dispatch_uid='waldur_mastermind.marketplace.update_category_offerings_count',
        )

        signals.post_save.connect(
            handlers.update_aggregate_resources_count_when_resource_is_updated,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.'
                         'update_aggregate_resources_count_when_resource_is_updated',
        )

        quota_signals.recalculate_quotas.connect(
            handlers.update_aggregate_resources_count,
            dispatch_uid='waldur_mastermind.marketplace.update_aggregate_resources_count',
        )

        signals.post_save.connect(
            handlers.close_resource_plan_period_when_resource_is_terminated,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.'
                         'close_resource_plan_period_when_resource_is_terminated',
        )

        marketplace_signals.limit_update_succeeded.connect(
            handlers.limit_update_succeeded,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.limit_update_succeeded',
        )

        marketplace_signals.limit_update_failed.connect(
            handlers.limit_update_failed,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.limit_update_failed',
        )

        for resource_serializer in SupportedServices.get_resource_serializers():
            core_signals.pre_serializer_fields.connect(
                sender=resource_serializer,
                receiver=utils.add_marketplace_offering,
            )
