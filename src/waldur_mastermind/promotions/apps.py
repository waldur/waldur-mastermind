from django.apps import AppConfig
from django.db.models import signals


def _create_discounted_resource_on_activation(resource):
    from waldur_mastermind.promotions import handlers

    handlers.create_discounted_resource_on_activation(resource)


def _get_discount_for_resource(resource, year, month, unit_price):
    from waldur_mastermind.promotions import models

    return models.Campaign.get_discount_for_resource(resource, year, month, unit_price)


def _count_active_campaigns(service_provider, today) -> int:
    from waldur_mastermind.promotions import handlers

    return handlers.count_active_campaigns(service_provider, today)


class BookingConfig(AppConfig):
    name = "waldur_mastermind.promotions"
    verbose_name = "Promotions"

    def ready(self):
        from waldur_mastermind.marketplace import hooks as marketplace_hooks
        from waldur_mastermind.promotions import handlers, models

        signals.post_save.connect(
            handlers.apply_campaign_to_pending_invoices,
            sender=models.Campaign,
            dispatch_uid="waldur_mastermind.promotions.apply_campaign_to_pending_invoices",
        )
        marketplace_hooks.register_promotions_provider(
            marketplace_hooks.PromotionsProvider(
                create_discounted_resource_on_activation=_create_discounted_resource_on_activation,
                get_discount_for_resource=_get_discount_for_resource,
                count_active_campaigns=_count_active_campaigns,
            )
        )
