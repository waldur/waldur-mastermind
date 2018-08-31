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
            sender=models.Screenshot,
            dispatch_uid='waldur_mastermind.marketplace.create_screenshot_thumbnail',
        )

        signals.post_save.connect(
            handlers.notifications_order_approval,
            sender=models.Order,
            dispatch_uid='waldur_mastermind.marketplace.notifications_order_approval',
        )

        signals.post_save.connect(
            handlers.order_set_state_done,
            sender=models.OrderItem,
            dispatch_uid='waldur_mastermind.marketplace.order_set_state_done',
        )
