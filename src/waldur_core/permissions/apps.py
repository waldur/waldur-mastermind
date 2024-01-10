from django.apps import AppConfig


class PermissionsConfig(AppConfig):
    name = "waldur_core.permissions"
    verbose_name = "Permissions"

    def ready(self):
        from . import handlers, signals

        signals.role_granted.connect(
            handlers.log_role_granted,
            dispatch_uid="waldur_core.permissions.log_role_granted",
        )

        signals.role_revoked.connect(
            handlers.log_role_revoked,
            dispatch_uid="waldur_core.permissions.log_role_revoked",
        )

        signals.role_updated.connect(
            handlers.log_role_updated,
            dispatch_uid="waldur_core.permissions.log_role_updated",
        )
