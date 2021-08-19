from django.apps import AppConfig
from django.db.models import signals


class AnalyticsConfig(AppConfig):
    name = 'waldur_mastermind.analytics'
    verbose_name = 'Analytics'

    def ready(self):
        from waldur_core.quotas.models import Quota

        from . import handlers

        signals.post_save.connect(
            handlers.update_daily_quotas,
            sender=Quota,
            dispatch_uid='waldur_mastermind.analytics.handlers.update_daily_quotas',
        )
