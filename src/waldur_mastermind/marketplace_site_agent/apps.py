from django.apps import AppConfig
from django.db.models import signals

from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING


class MarketplaceSlurmConfig(AppConfig):
    name = "waldur_mastermind.marketplace_site_agent"
    verbose_name = "Marketplace SLURM Remote"
    service_name = "SLURM remote"

    def ready(self):
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.structure import signals as structure_signals
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace_site_agent import (
            executors,
            handlers,
            models,
            processor,
        )

        manager.register(
            SITE_AGENT_OFFERING,
            create_resource_processor=processor.CreateAllocationProcessor,
            update_resource_processor=processor.UpdateAllocationLimitsProcessor,
            delete_resource_processor=processor.DeleteAllocationProcessor,
            can_update_limits=True,
            enable_remote_support=True,
            pull_resource_executor=executors.AgentResourcePullExecutor,
            supports_order_retry=True,
        )

        signals.post_save.connect(
            handlers.send_done_order_to_message_queue,
            sender=marketplace_models.Order,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_done_order_to_message_queue",
        )

        signals.post_save.connect(
            handlers.send_pending_order_to_message_queue,
            sender=marketplace_models.Order,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_pending_order_to_message_queue",
        )

        signals.post_save.connect(
            handlers.send_offering_user_username_message,
            sender=marketplace_models.OfferingUser,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_offering_user_username_message",
        )

        signals.post_save.connect(
            handlers.send_resource_update_message_to_queue,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_resource_update_message_to_queue",
        )

        permission_signals.role_granted.connect(
            handlers.send_role_granted_message_to_queue,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_role_granted_message_to_queue",
        )

        permission_signals.role_revoked.connect(
            handlers.send_role_revoked_message_to_queue,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_role_revoked_message_to_queue",
        )

        signals.post_save.connect(
            handlers.send_project_service_account_info,
            sender=marketplace_models.ProjectServiceAccount,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_project_service_account_info",
        )

        signals.post_save.connect(
            handlers.send_project_service_account_deletion_info,
            sender=marketplace_models.ProjectServiceAccount,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_project_service_account_deletion_info",
        )

        signals.post_save.connect(
            handlers.send_course_account_info,
            sender=marketplace_models.CourseAccount,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_course_account_info",
        )

        signals.post_save.connect(
            handlers.send_course_account_deletion_info,
            sender=marketplace_models.CourseAccount,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_course_account_deletion_info",
        )

        signals.pre_delete.connect(
            handlers.cleanup_agent_identity_queue,
            sender=models.AgentIdentity,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.cleanup_agent_identity_queue",
        )

        structure_signals.project_moved.connect(
            handlers.send_resource_messages_on_project_move,
            dispatch_uid="waldur_mastermind.marketplace_site_agent.send_resource_messages_on_project_move",
        )
