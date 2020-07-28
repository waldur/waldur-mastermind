from django.apps import AppConfig


class MarketplaceChecklistConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_checklist'
    verbose_name = 'Checklist'

    def ready(self):
        pass
