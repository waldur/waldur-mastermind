from __future__ import unicode_literals

from django.apps import AppConfig
from django.db.models import signals


class MarketplaceConfig(AppConfig):
    name = 'waldur_mastermind.marketplace'
    verbose_name = 'Marketplace'

    def ready(self):
        from . import handlers, models

        signals.post_save.connect(
            handlers.create_screenshot_thumbnail,
            sender=models.Screenshots,
            dispatch_uid='waldur_mastermind.marketplace.create_screenshot_thumbnail',
        )
