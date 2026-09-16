from django.apps import AppConfig
from django.db.models import signals


class SramConfig(AppConfig):
    name = "waldur_sram"
    verbose_name = "SRAM integration"

    def ready(self):
        from waldur_core.permissions.utils import register_quiet_grant_source
        from waldur_core.structure.models import Customer

        from . import handlers, roles

        # Membership syncs would otherwise email on every SRAM change.
        register_quiet_grant_source(roles.SOURCE_PREFIX)
        register_quiet_grant_source(roles.RULE_SOURCE_PREFIX)

        signals.post_save.connect(
            handlers.rename_placeholder_roles_on_slug_change,
            sender=Customer,
            dispatch_uid="waldur_sram.rename_placeholder_roles_on_slug_change",
        )
