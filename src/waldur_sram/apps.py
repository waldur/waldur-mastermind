from django.apps import AppConfig
from django.db.models import signals


class SramConfig(AppConfig):
    name = "waldur_sram"
    verbose_name = "SRAM integration"

    def ready(self):
        from waldur_core.structure.models import Customer

        from . import handlers

        signals.post_save.connect(
            handlers.rename_placeholder_roles_on_slug_change,
            sender=Customer,
            dispatch_uid="waldur_sram.rename_placeholder_roles_on_slug_change",
        )
