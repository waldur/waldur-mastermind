from django.apps import AppConfig


class PermissionsConfig(AppConfig):
    name = "waldur_core.permissions"
    verbose_name = "Permissions"

    def ready(self):
        from django.core.signals import request_finished, request_started
        from django.db.models.signals import post_delete, post_save, pre_save

        from waldur_core.structure.models import Customer

        from . import global_scope, handlers, models, signals

        signals.role_granted.connect(
            handlers.log_role_granted,
            dispatch_uid="waldur_core.permissions.log_role_granted",
        )

        signals.role_granted.connect(
            handlers.reactivate_user_if_gaining_roles,
            dispatch_uid="waldur_core.permissions.reactivate_user_if_gaining_roles",
        )

        signals.role_revoked.connect(
            handlers.log_role_revoked,
            dispatch_uid="waldur_core.permissions.log_role_revoked",
        )

        signals.role_revoked.connect(
            handlers.deactivate_user_if_no_roles,
            dispatch_uid="waldur_core.permissions.deactivate_user_if_no_roles",
        )

        signals.role_updated.connect(
            handlers.log_role_updated,
            dispatch_uid="waldur_core.permissions.log_role_updated",
        )

        post_save.connect(
            handlers.log_role_concealed,
            sender=models.CustomerRoleConcealment,
            dispatch_uid="waldur_core.permissions.log_role_concealed",
        )

        post_delete.connect(
            handlers.log_role_revealed,
            sender=models.CustomerRoleConcealment,
            dispatch_uid="waldur_core.permissions.log_role_revealed",
        )

        post_delete.connect(
            handlers.revoke_user_roles_on_availability_removal,
            sender=models.RoleAvailability,
            dispatch_uid="waldur_core.permissions.revoke_user_roles_on_availability_removal",
        )

        pre_save.connect(
            handlers.stash_customer_slug,
            sender=Customer,
            dispatch_uid="waldur_core.permissions.stash_customer_slug",
        )

        post_save.connect(
            handlers.rename_clones_on_customer_slug_change,
            sender=Customer,
            dispatch_uid="waldur_core.permissions.rename_clones_on_customer_slug_change",
        )

        # Site-wide grants: a per-request memo of each user's global
        # permissions, and a per-process note that the site scope exists. See
        # global_scope for why both are needed and what each costs.
        request_started.connect(
            global_scope.start_request,
            dispatch_uid="waldur_core.permissions.global_scope.start_request",
        )
        request_finished.connect(
            global_scope.end_request,
            dispatch_uid="waldur_core.permissions.global_scope.end_request",
        )
        post_save.connect(
            global_scope.on_scope_saved,
            sender=models.GlobalRoleScope,
            dispatch_uid="waldur_core.permissions.global_scope.on_scope_saved",
        )
        post_save.connect(
            global_scope.on_user_role_changed,
            sender=models.UserRole,
            dispatch_uid="waldur_core.permissions.global_scope.on_user_role_saved",
        )
        post_delete.connect(
            global_scope.on_user_role_changed,
            sender=models.UserRole,
            dispatch_uid="waldur_core.permissions.global_scope.on_user_role_deleted",
        )

        # Register per-model rules for the scoped-PAT list filter.
        # Deferred to ready() so cross-app model imports resolve.
        from . import pat_filtering

        pat_filtering._register_rules()
        pat_filtering._install_global_filter()
