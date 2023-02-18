from django.apps import AppConfig
from django.db.models import signals


class BookingConfig(AppConfig):
    name = 'waldur_mastermind.promotions'
    verbose_name = 'Promotions'

    def ready(self):
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.promotions import handlers, models

        signals.pre_save.connect(
            handlers.create_discounted_resource,
            sender=marketplace_models.Resource,
            dispatch_uid='waldur_mastermind.promotions.create_discounted_resource',
        )

        signals.post_save.connect(
            handlers.apply_campaign_to_pending_invoices,
            sender=models.Campaign,
            dispatch_uid='waldur_mastermind.promotions.apply_campaign_to_pending_invoices',
        )
