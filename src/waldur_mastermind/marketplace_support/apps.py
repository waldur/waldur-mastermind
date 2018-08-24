from django.apps import AppConfig
from django.db.models import signals


class MarketplaceSupportConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_support'
    verbose_name = 'Marketplace supports'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace_support import PLUGIN_NAME

        from . import handlers, processor, utils

        signals.post_save.connect(
            handlers.create_support_template,
            sender=marketplace_models.Offering,
            dispatch_uid='waldur_mastermind.marketpace_support.create_support_template',
        )

        manager.register(PLUGIN_NAME, processor.process_support, validator=utils.validate_options)
