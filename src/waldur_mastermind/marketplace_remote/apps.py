from django.apps import AppConfig
from django.db.models import signals


class MarketplaceRemoteConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_remote'
    verbose_name = 'Remote Marketplace'

    def ready(self):
        from waldur_core.structure import signals as structure_signals
        from waldur_core.structure.models import Customer, Project, ProjectPermission
        from waldur_mastermind.marketplace import models, plugins
        from waldur_mastermind.marketplace_remote import (
            PLUGIN_NAME,
            constants,
            processors,
        )

        from . import handlers

        ProjectUpdateRequest = self.get_model('ProjectUpdateRequest')

        plugins.manager.register(
            offering_type=PLUGIN_NAME,
            create_resource_processor=processors.RemoteCreateResourceProcessor,
            update_resource_processor=processors.RemoteUpdateResourceProcessor,
            delete_resource_processor=processors.RemoteDeleteResourceProcessor,
            can_update_limits=True,
            can_manage_offering_components=False,
            plan_fields_that_cannot_be_edited=constants.PLAN_FIELDS,
            can_manage_plans=False,
        )

        structure_signals.structure_role_granted.connect(
            handlers.sync_permission_with_remote_project,
            sender=Project,
            dispatch_uid='marketplace_remote.sync_permission_with_remote_project_granting',
        )

        structure_signals.structure_role_revoked.connect(
            handlers.sync_permission_with_remote_project,
            sender=Project,
            dispatch_uid='marketplace_remote.sync_permission_with_remote_project_revoking',
        )

        structure_signals.structure_role_granted.connect(
            handlers.sync_permission_with_remote_customer,
            sender=Customer,
            dispatch_uid='marketplace_remote.sync_permission_with_remote_customer_granting',
        )

        structure_signals.structure_role_revoked.connect(
            handlers.sync_permission_with_remote_customer,
            sender=Customer,
            dispatch_uid='marketplace_remote.sync_permission_with_remote_customer_revoking',
        )

        structure_signals.structure_role_updated.connect(
            handlers.update_remote_project_permission,
            sender=ProjectPermission,
            dispatch_uid='marketplace_remote.update_remote_project_permission',
        )

        signals.post_save.connect(
            handlers.create_request_when_project_is_updated,
            sender=Project,
            dispatch_uid='marketplace_remote.create_request_when_project_is_updated',
        )

        signals.post_save.connect(
            handlers.sync_remote_project_when_request_is_approved,
            sender=ProjectUpdateRequest,
            dispatch_uid='marketplace_remote.sync_remote_project_when_request_is_approved',
        )

        signals.post_save.connect(
            handlers.log_request_events,
            sender=ProjectUpdateRequest,
            dispatch_uid='marketplace_remote.log_request_events',
        )

        signals.post_save.connect(
            handlers.notify_about_project_details_update,
            sender=ProjectUpdateRequest,
            dispatch_uid='marketplace_remote.notify_about_project_details_update',
        )

        signals.post_delete.connect(
            handlers.delete_remote_project,
            sender=Project,
            dispatch_uid='marketplace_remote.delete_remote_project',
        )

        signals.post_save.connect(
            handlers.trigger_order_item_callback,
            sender=models.OrderItem,
            dispatch_uid='marketplace_remote.trigger_order_item_notification',
        )
