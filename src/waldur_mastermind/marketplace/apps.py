from django.apps import AppConfig
from django.db.models import signals


class MarketplaceConfig(AppConfig):
    name = 'waldur_mastermind.marketplace'
    verbose_name = 'Marketplace'

    def ready(self):
        from waldur_core.core import signals as core_signals
        from waldur_core.quotas import signals as quota_signals
        from waldur_core.structure import models as structure_models
        from waldur_core.structure import signals as structure_signals
        from waldur_core.structure.serializers import BaseResourceSerializer

        from . import (
            handlers,
            models,
            utils,
            signals as marketplace_signals,
            processors,
            registrators as marketplace_registrators,
            PLUGIN_NAME,
        )
        from .plugins import manager

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

        marketplace_signals.resource_limit_update_succeeded.connect(
            handlers.limit_update_succeeded,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.resource_limit_update_succeeded',
        )

        marketplace_signals.resource_limit_update_failed.connect(
            handlers.limit_update_failed,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.resource_limit_update_failed',
        )

        for resource_serializer in BaseResourceSerializer.get_subclasses():
            core_signals.pre_serializer_fields.connect(
                sender=resource_serializer, receiver=utils.add_marketplace_offering,
            )

        signals.post_save.connect(
            handlers.disable_archived_service_settings_without_existing_resource,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.disable_archived_service_settings_without_existing_resource',
        )

        signals.post_save.connect(
            handlers.disable_service_settings_without_existing_resource_when_archived,
            sender=models.Offering,
            dispatch_uid='waldur_mastermind.marketplace.disable_service_settings_without_existing_resource_when_archived',
        )

        signals.post_save.connect(
            handlers.enable_service_settings_with_existing_resource,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.enable_service_settings_whith_existing_resource',
        )

        signals.post_save.connect(
            handlers.enable_service_settings_when_not_archived,
            sender=models.Offering,
            dispatch_uid='waldur_mastermind.marketplace.enable_service_settings_when_not_archived',
        )

        manager.register(
            offering_type=PLUGIN_NAME,
            create_resource_processor=processors.BasicCreateResourceProcessor,
            update_resource_processor=processors.BasicUpdateResourceProcessor,
            delete_resource_processor=processors.BasicDeleteResourceProcessor,
            enable_usage_notifications=True,
            enable_remote_support=True,
            can_update_limits=True,
        )

        marketplace_registrators.MarketplaceRegistrator.connect()

        structure_signals.structure_role_granted.connect(
            handlers.log_offering_permission_granted,
            sender=models.Offering,
            dispatch_uid='waldur_mastermind.marketplace.log_offering_permission_granted',
        )

        structure_signals.structure_role_revoked.connect(
            handlers.log_offering_permission_revoked,
            sender=models.Offering,
            dispatch_uid='waldur_mastermind.marketplace.log_offering_permission_revoked',
        )

        structure_signals.structure_role_updated.connect(
            handlers.log_offering_permission_updated,
            sender=models.OfferingPermission,
            dispatch_uid='waldur_mastermind.marketplace.log_offering_permission_updated',
        )

        structure_signals.structure_role_granted.connect(
            handlers.add_service_manager_role_to_customer,
            sender=models.Offering,
            dispatch_uid='waldur_mastermind.marketplace.add_service_manager_role_to_customer',
        )

        structure_signals.structure_role_revoked.connect(
            handlers.drop_service_manager_role_from_customer,
            sender=models.Offering,
            dispatch_uid='waldur_mastermind.marketplace.drop_service_manager_role_from_customer',
        )

        structure_signals.structure_role_revoked.connect(
            handlers.drop_offering_permissions_if_service_manager_role_is_revoked,
            sender=structure_models.Customer,
            dispatch_uid='waldur_mastermind.marketplace.drop_offering_permissions_if_service_manager_role_is_revoked',
        )

        signals.post_save.connect(
            handlers.resource_has_been_renamed,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.resource_has_been_renamed',
        )

        signals.post_save.connect(
            handlers.delete_expired_project_if_every_resource_has_been_terminated,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.'
            'delete_expired_project_if_every_resource_has_been_terminated',
        )
