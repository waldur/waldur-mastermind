from django.apps import AppConfig
from django.db.models import signals


class MarketplaceRemoteConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_remote'
    verbose_name = 'Remote Marketplace'

    def ready(self):
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.structure.models import Project
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

        permission_signals.role_granted.connect(
            handlers.sync_permission_with_remote,
            dispatch_uid='marketplace_remote.sync_permission_when_role_granted',
        )

        permission_signals.role_revoked.connect(
            handlers.sync_permission_with_remote,
            dispatch_uid='marketplace_remote.sync_permission_when_role_revoked',
        )

        permission_signals.role_updated.connect(
            handlers.sync_permission_with_remote,
            dispatch_uid='marketplace_remote.sync_permission_when_role_updated',
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
