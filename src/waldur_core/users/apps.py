from django.apps import AppConfig
from django.db.models import signals


class UserConfig(AppConfig):
    name = "waldur_core.users"
    verbose_name = "Users"

    def ready(self):
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.structure.models import Project
        from waldur_core.users import handlers
        from waldur_core.users.models import PermissionRequest
        from waldur_core.users.scim import handlers as scim_handlers

        signals.pre_delete.connect(
            handlers.cancel_invitations_on_project_deletion,
            sender=Project,
            dispatch_uid="waldur_core.users.handlers.cancel_invitations_on_project_deletion",
        )

        signals.post_save.connect(
            handlers.create_notification_about_permission_request_has_been_submitted,
            sender=PermissionRequest,
            dispatch_uid="waldur_core.users.handlers.create_notification_about_permission_request_has_been_submited",
        )

        signals.post_save.connect(
            handlers.create_notification_about_permission_request_has_been_rejected,
            sender=PermissionRequest,
            dispatch_uid="waldur_core.users.handlers.create_notification_about_permission_request_has_been_rejected",
        )

        permission_signals.role_granted.connect(
            scim_handlers.schedule_user_sync,
            dispatch_uid="waldur_core.users.scim.handlers.schedule_user_sync_on_role_granted",
        )
        permission_signals.role_revoked.connect(
            scim_handlers.schedule_user_sync,
            dispatch_uid="waldur_core.users.scim.handlers.schedule_user_sync_on_role_revoked",
        )
