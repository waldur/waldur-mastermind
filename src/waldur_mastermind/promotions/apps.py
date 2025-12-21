from django.apps import AppConfig
from django.db.models import signals


class BookingConfig(AppConfig):
    name = "waldur_mastermind.promotions"
    verbose_name = "Promotions"

    def ready(self):
        from waldur_mastermind.promotions import handlers, models

        signals.post_save.connect(
            handlers.apply_campaign_to_pending_invoices,
            sender=models.Campaign,
            dispatch_uid="waldur_mastermind.promotions.apply_campaign_to_pending_invoices",
        )
