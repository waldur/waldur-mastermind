from django.apps import AppConfig
from django.db.models import signals

from .enums import BASIC_OFFERING


class MarketplaceConfig(AppConfig):
    name = "waldur_mastermind.marketplace"
    verbose_name = "Marketplace"

    def ready(self):
        from waldur_core.core import models as core_models
        from waldur_core.core import signals as core_signals
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.quotas import signals as quota_signals
        from waldur_core.structure import models as structure_models
        from waldur_core.structure import signals as structure_signals
        from waldur_core.structure.serializers import BaseResourceSerializer
        from waldur_freeipa import models as freeipa_models
        from waldur_mastermind.marketplace.billing_usage import (
            schedule_component_usage_billing,
        )
        from waldur_mastermind.marketplace.handlers import (
            process_billing_on_resource_save,
        )

        from . import handlers, models, processors, utils
        from . import signals as marketplace_signals
        from .plugins import manager

        signals.post_save.connect(
            process_billing_on_resource_save,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.process_billing_on_resource_save",
        )

        # OfferingProfile sync — schedule async reconciliation when profile
        # roles change or when an offering is bound/unbound.
        signals.m2m_changed.connect(
            handlers.reconcile_offering_profile_on_roles_changed,
            sender=models.OfferingProfile.roles.through,
            dispatch_uid="waldur_mastermind.marketplace.reconcile_offering_profile_on_roles_changed",
        )
        signals.post_save.connect(
            handlers.reconcile_offering_profile_on_offering_changed,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.marketplace.reconcile_offering_profile_on_offering_changed",
        )

        from waldur_core.core.handlers import create_initial_revision

        for model in (models.Resource, models.Offering, models.Plan):
            signals.post_save.connect(
                create_initial_revision,
                sender=model,
                dispatch_uid=f"waldur_mastermind.marketplace.create_initial_revision_{model.__name__}",
            )

        signals.post_save.connect(
            schedule_component_usage_billing,
            sender=models.ComponentUsage,
            dispatch_uid="waldur_mastermind.marketplace."
            "schedule_component_usage_billing",
        )

        signals.post_save.connect(
            handlers.sync_current_usages_from_component_usage,
            sender=models.ComponentUsage,
            dispatch_uid="waldur_mastermind.marketplace.sync_current_usages",
        )

        signals.post_save.connect(
            handlers.create_screenshot_thumbnail,
            sender=models.Screenshot,
            dispatch_uid="waldur_mastermind.marketplace.create_screenshot_thumbnail",
        )

        signals.post_save.connect(
            handlers.log_order_events,
            sender=models.Order,
            dispatch_uid="waldur_mastermind.marketplace.log_order_events",
        )

        signals.post_save.connect(
            handlers.notify_approvers_when_order_is_created,
            sender=models.Order,
            dispatch_uid="waldur_mastermind.marketplace.notify_approvers_when_order_is_created",
        )

        signals.post_save.connect(
            handlers.maybe_auto_approve_order_for_project,
            sender=models.Order,
            dispatch_uid="waldur_mastermind.marketplace.maybe_auto_approve_order_for_project",
        )

        signals.post_save.connect(
            handlers.update_resource_state_on_order_creation,
            sender=models.Order,
            dispatch_uid="waldur_mastermind.marketplace.update_resource_state_on_order_creation",
        )

        signals.post_save.connect(
            handlers.update_resource_state_on_order_rejection_error_or_cancellation,
            sender=models.Order,
            dispatch_uid="waldur_mastermind.marketplace.update_resource_state_on_order_rejection_error_or_cancellation",
        )

        signals.post_save.connect(
            handlers.sync_resource_limit_when_order,
            sender=models.Order,
            dispatch_uid="waldur_mastermind.marketplace.sync_resource_limit_when_order",
        )

        signals.post_save.connect(
            handlers.notify_user_about_rejected_order,
            sender=models.Order,
            dispatch_uid="waldur_mastermind.marketplace.notify_user_about_rejected_order",
        )

        signals.post_save.connect(
            handlers.trigger_user_action_recalculation_on_order_state_change,
            sender=models.Order,
            dispatch_uid="waldur_mastermind.marketplace.trigger_user_action_recalculation_on_order_state_change",
        )

        signals.post_save.connect(
            handlers.log_resource_events,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.log_resource_events",
        )

        signals.post_save.connect(
            handlers.init_resource_parent,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.init_resource_parent",
        )

        signals.post_save.connect(
            handlers.update_category_quota_when_offering_is_created,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.marketplace.update_category_quota_when_offering_is_created",
        )
        signals.pre_delete.connect(
            handlers.close_service_accounts_on_project_deletion,
            sender=structure_models.Project,
            dispatch_uid="waldur_mastermind.marketplace.close_service_accounts_on_project_deletion",
        )

        signals.pre_delete.connect(
            handlers.close_customer_service_accounts_on_customer_deletion,
            sender=structure_models.Customer,
            dispatch_uid="waldur_mastermind.marketplace.close_customer_service_accounts_on_customer_deletion",
        )

        signals.pre_delete.connect(
            handlers.revoke_roles_on_offering_deletion,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.marketplace.revoke_roles_on_offering_deletion",
        )

        signals.post_delete.connect(
            handlers.update_category_quota_when_offering_is_deleted,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.marketplace.update_category_quota_when_offering_is_deleted",
        )

        signals.post_delete.connect(
            handlers.delete_service_setting_when_offering_is_deleted,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.marketplace.delete_service_setting_when_offering_is_deleted",
        )

        quota_signals.recalculate_quotas.connect(
            handlers.update_category_offerings_count,
            dispatch_uid="waldur_mastermind.marketplace.update_category_offerings_count",
        )

        signals.post_save.connect(
            handlers.create_resource_plan_period_when_resource_is_created,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace."
            "create_resource_plan_period_when_resource_is_created",
        )

        signals.post_save.connect(
            handlers.close_resource_plan_period_when_resource_is_terminated,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace."
            "close_resource_plan_period_when_resource_is_terminated",
        )

        signals.post_save.connect(
            handlers.soft_delete_resource_projects_when_resource_is_terminated,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace."
            "soft_delete_resource_projects_when_resource_is_terminated",
        )

        signals.post_save.connect(
            handlers.switch_resource_plan_period_when_plan_is_updated,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace."
            "switch_resource_plan_period_when_plan_is_updated",
        )

        signals.post_save.connect(
            handlers.sync_limits,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.sync_limits",
        )

        marketplace_signals.resource_limit_update_succeeded.connect(
            handlers.limit_update_succeeded,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.resource_limit_update_succeeded",
        )

        marketplace_signals.resource_limit_update_failed.connect(
            handlers.limit_update_failed,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.resource_limit_update_failed",
        )

        for resource_serializer in BaseResourceSerializer.get_subclasses():
            core_signals.pre_serializer_fields.connect(
                sender=resource_serializer,
                receiver=utils.add_marketplace_offering,
            )

        signals.post_save.connect(
            handlers.disable_archived_service_settings_without_existing_resource,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.disable_archived_service_settings_without_existing_resource",
        )

        signals.post_save.connect(
            handlers.disable_service_settings_without_existing_resource_when_archived,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.marketplace.disable_service_settings_without_existing_resource_when_archived",
        )

        signals.post_save.connect(
            handlers.enable_service_settings_with_existing_resource,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.enable_service_settings_whith_existing_resource",
        )

        signals.post_save.connect(
            handlers.enable_service_settings_when_not_archived,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.marketplace.enable_service_settings_when_not_archived",
        )

        signals.post_save.connect(
            handlers.plan_component_has_been_updated,
            sender=models.PlanComponent,
            dispatch_uid="waldur_mastermind.plan_component_has_been_updated",
        )

        signals.post_save.connect(
            handlers.offering_component_has_been_created_or_updated,
            sender=models.OfferingComponent,
            dispatch_uid="waldur_mastermind.offering_component_has_been_created_or_updated",
        )

        signals.post_delete.connect(
            handlers.offering_component_has_been_deleted,
            sender=models.OfferingComponent,
            dispatch_uid="waldur_mastermind.offering_component_has_been_deleted",
        )

        signals.post_save.connect(
            handlers.plan_has_been_created_or_updated,
            sender=models.Plan,
            dispatch_uid="waldur_mastermind.plan_has_been_created_or_updated",
        )

        signals.post_save.connect(
            handlers.offering_has_been_created_or_updated,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.plan_has_been_created_or_updated",
        )

        signals.post_save.connect(
            handlers.create_checklist_completions_for_existing_users,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.marketplace.create_checklist_completions_for_existing_users",
        )

        manager.register(
            offering_type=BASIC_OFFERING,
            create_resource_processor=processors.BasicCreateResourceProcessor,
            update_resource_processor=processors.BasicUpdateResourceProcessor,
            delete_resource_processor=processors.BasicDeleteResourceProcessor,
            enable_usage_notifications=True,
            enable_remote_support=True,
            can_update_limits=True,
            can_terminate_order=True,
            supports_order_retry=True,
        )

        structure_signals.project_moved.connect(
            handlers.update_customer_of_offering_if_project_has_been_moved,
            sender=structure_models.Project,
            dispatch_uid="waldur_mastermind.marketplace.update_customer_of_offering_if_project_has_been_moved",
        )

        signals.post_save.connect(
            handlers.process_invitations_and_orders_when_project_start_date_is_unset,
            sender=structure_models.Project,
            dispatch_uid="waldur_mastermind.marketplace.process_invitations_and_orders_when_project_start_date_is_unset",
        )

        signals.post_save.connect(
            handlers.resource_state_has_been_changed,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.resource_state_has_been_changed",
        )
        signals.post_save.connect(
            handlers.trigger_scim_sync_on_resource_ok,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.trigger_scim_sync_on_resource_ok",
        )

        signals.post_save.connect(
            handlers.resource_has_been_changed,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.resource_has_been_changed",
        )

        signals.post_save.connect(
            handlers.delete_expired_project_if_every_resource_has_been_terminated,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace."
            "delete_expired_project_if_every_resource_has_been_terminated",
        )

        signals.post_save.connect(
            handlers.log_offering_user_created,
            sender=models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace.log_offering_user_created",
        )

        signals.post_delete.connect(
            handlers.log_offering_user_deleted,
            sender=models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace.log_offering_user_deleted",
        )

        signals.post_save.connect(
            handlers.log_offering_user_username_updated,
            sender=models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace.log_offering_user_username_updated",
        )

        signals.post_save.connect(
            handlers.create_offering_user_checklist_completions,
            sender=models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace.create_offering_user_checklist_completions",
        )

        signals.pre_delete.connect(
            handlers.delete_offering_user_checklist_completions,
            sender=models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace.delete_offering_user_checklist_completions",
        )

        signals.post_save.connect(
            handlers.send_offering_user_created_message,
            sender=models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace.send_offering_user_created_message",
        )

        signals.post_save.connect(
            handlers.send_offering_user_updated_message,
            sender=models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace.send_offering_user_updated_message",
        )

        signals.post_save.connect(
            handlers.trigger_scim_sync_on_offering_user_ok,
            sender=models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace.trigger_scim_sync_on_offering_user_ok",
        )

        signals.post_delete.connect(
            handlers.send_offering_user_deleted_message,
            sender=models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace.send_offering_user_deleted_message",
        )

        signals.post_save.connect(
            handlers.log_resource_robot_account_created_or_updated,
            sender=models.RobotAccount,
            dispatch_uid="waldur_core.marketplace.handlers.log_resource_robot_account_created_or_updated",
        )

        signals.post_save.connect(
            handlers.log_service_account_created_or_updated,
            sender=models.ScopedServiceAccount,
            dispatch_uid="waldur_core.marketplace.handlers.log_service_account_created_or_updated",
        )

        signals.post_delete.connect(
            handlers.log_resource_robot_account_deleted,
            sender=models.RobotAccount,
            dispatch_uid="waldur_core.marketplace.handlers.log_resource_robot_account_deleted",
        )

        signals.post_delete.connect(
            handlers.log_service_account_deleted,
            sender=models.ScopedServiceAccount,
            dispatch_uid="waldur_core.marketplace.handlers.log_service_account_deleted",
        )

        signals.post_delete.connect(
            handlers.purge_offering_role_groups_on_scope_delete,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.purge_role_groups_on_resource_delete",
        )
        signals.post_delete.connect(
            handlers.purge_offering_role_groups_on_scope_delete,
            sender=models.ResourceProject,
            dispatch_uid="waldur_mastermind.marketplace.purge_role_groups_on_rp_delete",
        )

        permission_signals.role_granted.connect(
            handlers.create_offering_users_when_project_role_granted,
            dispatch_uid="waldur_mastermind.marketplace.create_offering_user_when_project_role_created",
        )

        permission_signals.role_revoked.connect(
            handlers.request_offering_user_deletion_when_project_access_lost,
            dispatch_uid="waldur_mastermind.marketplace.request_offering_user_deletion_when_project_access_lost",
        )

        permission_signals.role_revoked.connect(
            handlers.handle_user_role_revoked,
            dispatch_uid="waldur_mastermind.marketplace.handle_user_role_revoked",
        )

        # MaintenanceAnnouncement -> AdminAnnouncement handlers
        signals.post_save.connect(
            handlers.manage_maintenance_admin_announcements,
            sender=models.MaintenanceAnnouncement,
            dispatch_uid="waldur_mastermind.marketplace.manage_maintenance_admin_announcements",
        )

        signals.post_save.connect(
            handlers.update_maintenance_announcement_on_offering_change,
            sender=models.MaintenanceAnnouncementOffering,
            dispatch_uid="waldur_mastermind.marketplace.update_maintenance_announcement_on_offering_change",
        )

        signals.post_delete.connect(
            handlers.update_maintenance_announcement_on_offering_change,
            sender=models.MaintenanceAnnouncementOffering,
            dispatch_uid="waldur_mastermind.marketplace.update_maintenance_announcement_on_offering_delete",
        )

        signals.pre_delete.connect(
            handlers.cleanup_admin_announcement_on_maintenance_deletion,
            sender=models.MaintenanceAnnouncement,
            dispatch_uid="waldur_mastermind.marketplace.cleanup_admin_announcement_on_maintenance_deletion",
        )

        signals.post_save.connect(
            handlers.create_offering_users_if_order_is_valid,
            sender=models.Order,
            dispatch_uid="waldur_mastermind.marketplace.create_offering_users_when_order_becomes_pending_provider",
        )

        marketplace_signals.resource_creation_succeeded.connect(
            handlers.create_offering_user_for_new_resource,
            sender=models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.create_offering_user_for_new_resource",
        )

        signals.post_save.connect(
            handlers.update_offering_user_username_after_offering_settings_change,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.marketplace.update_offering_user_username_after_offering_settings_change",
        )

        signals.post_save.connect(
            handlers.update_offering_user_username_after_freeipa_profile_update,
            sender=freeipa_models.Profile,
            dispatch_uid="waldur_mastermind.marketplace.update_offering_user_username_after_freeipa_profile_update",
        )

        signals.post_save.connect(
            handlers.update_offering_user_username_after_user_change,
            sender=core_models.User,
            dispatch_uid="waldur_mastermind.marketplace.update_offering_user_username_after_user_change",
        )

        signals.post_save.connect(
            handlers.send_user_attribute_update_message,
            sender=core_models.User,
            dispatch_uid="waldur_mastermind.marketplace.send_user_attribute_update_message",
        )

        # Add maintenance fields to AdminAnnouncementSerializer
        from waldur_mastermind.notifications.serializers import (
            AdminAnnouncementSerializer,
        )

        core_signals.pre_serializer_fields.connect(
            handlers.add_maintenance_fields_to_admin_announcement_serializer,
            sender=AdminAnnouncementSerializer,
            dispatch_uid="waldur_mastermind.marketplace.add_maintenance_fields_to_admin_announcement_serializer",
        )

        signals.pre_delete.connect(
            handlers.close_course_accounts_after_project_removal,
            sender=structure_models.Project,
            dispatch_uid="waldur_mastermind.marketplace.close_course_accounts_after_project_removal",
        )
        signals.post_save.connect(
            handlers.log_terms_of_service_consent_granted,
            sender=models.UserOfferingConsent,
            dispatch_uid="waldur_mastermind.marketplace.log_terms_of_service_consent_granted",
        )
        signals.post_save.connect(
            handlers.log_terms_of_service_consent_revoked,
            sender=models.UserOfferingConsent,
            dispatch_uid="waldur_mastermind.marketplace.log_terms_of_service_consent_revoked",
        )

        signals.post_save.connect(
            handlers.notify_users_about_tos_update_signal,
            sender=models.OfferingTermsOfService,
            dispatch_uid="waldur_mastermind.marketplace.notify_users_about_tos_update_signal",
        )
        signals.post_save.connect(
            handlers.notify_offering_user_about_tos_requirement,
            sender=models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace.notify_offering_user_about_tos_requirement",
        )
        signals.post_save.connect(
            handlers.update_resource_scope_availability_on_offering_state_change,
            sender=models.Offering,
            dispatch_uid="waldur_mastermind.marketplace.update_resource_scope_availability_on_offering_state_change",
        )

        signals.post_save.connect(
            handlers.trigger_scim_sync_on_offering_endpoint_change,
            sender=models.OfferingAccessEndpoint,
            dispatch_uid="waldur_mastermind.marketplace.trigger_scim_sync_on_offering_endpoint_save",
        )
        signals.post_delete.connect(
            handlers.trigger_scim_sync_on_offering_endpoint_change,
            sender=models.OfferingAccessEndpoint,
            dispatch_uid="waldur_mastermind.marketplace.trigger_scim_sync_on_offering_endpoint_delete",
        )

        signals.post_save.connect(
            handlers.log_resource_limit_change_request_events,
            sender=models.ResourceLimitChangeRequest,
            dispatch_uid="waldur_mastermind.marketplace.log_resource_limit_change_request_events",
        )

        # Register user action cleanup handlers for marketplace models
        from waldur_core.user_actions.handlers import register_cleanup_handler

        register_cleanup_handler(
            models.Order, models.Offering, models.Resource, models.OfferingUser
        )
