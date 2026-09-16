from django.apps import AppConfig
from django.db.models import signals


class SramConfig(AppConfig):
    name = "waldur_sram"
    verbose_name = "SRAM integration"

    def ready(self):
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.permissions.utils import register_quiet_grant_source
        from waldur_core.structure.models import Customer, Project

        from . import handlers, models, roles

        # Membership syncs would otherwise email on every SRAM change.
        register_quiet_grant_source(roles.SOURCE_PREFIX)
        register_quiet_grant_source(roles.RULE_SOURCE_PREFIX)

        signals.post_save.connect(
            handlers.rename_placeholder_roles_on_slug_change,
            sender=Customer,
            dispatch_uid="waldur_sram.rename_placeholder_roles_on_slug_change",
        )
        permission_signals.role_granted.connect(
            handlers.reconcile_rules_on_placeholder_change,
            dispatch_uid="waldur_sram.reconcile_rules_on_placeholder_granted",
        )
        permission_signals.role_revoked.connect(
            handlers.reconcile_rules_on_placeholder_change,
            dispatch_uid="waldur_sram.reconcile_rules_on_placeholder_revoked",
        )
        signals.post_save.connect(
            handlers.reconcile_rules_on_project_change,
            sender=Project,
            dispatch_uid="waldur_sram.reconcile_rules_on_project_change",
        )
        signals.post_save.connect(
            handlers.reconcile_rule_on_save,
            sender=models.SramProjectRule,
            dispatch_uid="waldur_sram.reconcile_rule_on_save",
        )
        signals.post_delete.connect(
            handlers.revoke_rule_on_delete,
            sender=models.SramProjectRule,
            dispatch_uid="waldur_sram.revoke_rule_on_delete",
        )
