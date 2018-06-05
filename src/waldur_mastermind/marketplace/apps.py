from __future__ import unicode_literals

from django.apps import AppConfig


class MarketplaceConfig(AppConfig):
    name = 'waldur_mastermind.marketplace'
    verbose_name = 'Marketplaces'

    def ready(self):
        pass
