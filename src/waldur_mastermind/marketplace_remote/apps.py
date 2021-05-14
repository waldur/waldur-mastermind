from django.apps import AppConfig


class MarketplaceRemoteConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_remote'
    verbose_name = 'Remote Marketplace'

    def ready(self):
        from waldur_mastermind.marketplace import plugins
        from waldur_mastermind.marketplace_remote import PLUGIN_NAME
        from waldur_mastermind.marketplace_remote import processors
        from waldur_core.structure import signals as structure_signals
        from waldur_core.structure.models import Customer, Project

        from . import handlers

        plugins.manager.register(
            offering_type=PLUGIN_NAME,
            create_resource_processor=processors.RemoteCreateResourceProcessor,
            update_resource_processor=processors.RemoteUpdateResourceProcessor,
            delete_resource_processor=processors.RemoteDeleteResourceProcessor,
            can_update_limits=True,
        )

        structure_signals.structure_role_granted.connect(
            handlers.sync_permission_with_remote_project,
            sender=Project,
            dispatch_uid='waldur_core.structure.handlers.sync_permission_with_remote_project_granting',
        )

        structure_signals.structure_role_revoked.connect(
            handlers.sync_permission_with_remote_project,
            sender=Project,
            dispatch_uid='waldur_core.structure.handlers.sync_permission_with_remote_project_revoking',
        )

        structure_signals.structure_role_granted.connect(
            handlers.sync_permission_with_remote_customer,
            sender=Customer,
            dispatch_uid='waldur_core.structure.handlers.sync_permission_with_remote_customer_granting',
        )

        structure_signals.structure_role_revoked.connect(
            handlers.sync_permission_with_remote_customer,
            sender=Customer,
            dispatch_uid='waldur_core.structure.handlers.sync_permission_with_remote_customer_revoking',
        )
