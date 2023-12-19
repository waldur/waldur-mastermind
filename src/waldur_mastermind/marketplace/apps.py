from django.apps import AppConfig
from django.db.models import signals


class MarketplaceConfig(AppConfig):
    name = 'waldur_mastermind.marketplace'
    verbose_name = 'Marketplace'

    def ready(self):
        from waldur_core.core import signals as core_signals
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.quotas import signals as quota_signals
        from waldur_core.structure import models as structure_models
        from waldur_core.structure import signals as structure_signals
        from waldur_core.structure.serializers import BaseResourceSerializer

        from . import PLUGIN_NAME, handlers, models, processors
        from . import registrators as marketplace_registrators
        from . import signals as marketplace_signals
        from . import utils
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
            handlers.notify_approvers_when_order_is_created,
            sender=models.Order,
            dispatch_uid='waldur_mastermind.marketplace.notify_approvers_when_order_is_created',
        )

        signals.post_save.connect(
            handlers.update_resource_when_order_is_rejected,
            sender=models.Order,
            dispatch_uid='waldur_mastermind.marketplace.update_resource_when_order_is_rejected',
        )

        signals.post_save.connect(
            handlers.sync_resource_limit_when_order,
            sender=models.Order,
            dispatch_uid='waldur_mastermind.marketplace.sync_resource_limit_when_order',
        )

        signals.post_save.connect(
            handlers.log_resource_events,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.log_resource_events',
        )

        signals.post_save.connect(
            handlers.init_resource_parent,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.init_resource_parent',
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

        signals.post_save.connect(
            handlers.sync_limits,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.sync_limits',
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
                sender=resource_serializer,
                receiver=utils.add_marketplace_offering,
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
            can_terminate_order=True,
        )

        marketplace_registrators.MarketplaceRegistrator.connect()

        permission_signals.role_granted.connect(
            handlers.add_service_manager_role_to_customer,
            dispatch_uid='waldur_mastermind.marketplace.add_service_manager_role_to_customer',
        )

        permission_signals.role_revoked.connect(
            handlers.drop_service_manager_role_from_customer,
            dispatch_uid='waldur_mastermind.marketplace.drop_service_manager_role_from_customer',
        )

        structure_signals.project_moved.connect(
            handlers.update_customer_of_offering_if_project_has_been_moved,
            sender=structure_models.Project,
            dispatch_uid='waldur_mastermind.marketplace.update_customer_of_offering_if_project_has_been_moved',
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

        signals.post_save.connect(
            handlers.log_offering_user_created,
            sender=models.OfferingUser,
            dispatch_uid='waldur_mastermind.marketplace.log_offering_user_created',
        )

        signals.post_delete.connect(
            handlers.log_offering_user_deleted,
            sender=models.OfferingUser,
            dispatch_uid='waldur_mastermind.marketplace.log_offering_user_deleted',
        )

        signals.post_save.connect(
            handlers.log_resource_robot_account_created_or_updated,
            sender=models.RobotAccount,
            dispatch_uid='waldur_core.marketplace.handlers.log_resource_robot_account_created_or_updated',
        )

        signals.post_delete.connect(
            handlers.log_resource_robot_account_deleted,
            sender=models.RobotAccount,
            dispatch_uid='waldur_core.marketplace.handlers.log_resource_robot_account_deleted',
        )

        permission_signals.role_granted.connect(
            handlers.create_offering_users_when_project_role_granted,
            dispatch_uid='waldur_mastermind.marketplace.create_offering_user_when_project_role_created',
        )

        marketplace_signals.resource_creation_succeeded.connect(
            handlers.create_offering_user_for_new_resource,
            sender=models.Resource,
            dispatch_uid='waldur_mastermind.marketplace.create_offering_user_for_new_resource',
        )

        signals.post_save.connect(
            handlers.update_offering_user_username_after_offering_settings_change,
            sender=models.Offering,
            dispatch_uid='waldur_mastermind.marketplace.update_offering_user_username_after_offering_settings_change',
        )
