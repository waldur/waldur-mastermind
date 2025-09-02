from django.apps import AppConfig
from django.db.models import signals

from ..marketplace.enums import SCRIPT_OFFERING


class MarketplaceScriptConfig(AppConfig):
    name = "waldur_mastermind.marketplace_script"
    verbose_name = "Marketplace Script"

    def ready(self):
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import manager

        from . import handlers, processors
        from . import registrators as script_registrators

        manager.register(
            offering_type=SCRIPT_OFFERING,
            create_resource_processor=processors.CreateProcessor,
            update_resource_processor=processors.UpdateProcessor,
            delete_resource_processor=processors.DeleteProcessor,
            can_update_limits=True,
            is_interruptible=True,
        )

        script_registrators.ScriptRegistrator.connect()

        signals.post_save.connect(
            handlers.resource_options_have_been_changed,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_mastermind.marketplace.resource_options_have_been_changed",
        )
