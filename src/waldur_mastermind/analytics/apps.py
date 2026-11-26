from django.apps import AppConfig
from django.db.models import signals


def _record_daily_quotas(entries) -> None:
    from . import handlers

    handlers.record_daily_quotas(entries)


def _get_daily_quota_history(scope, name: str) -> list[dict]:
    from . import handlers

    return handlers.get_daily_quota_history(scope, name)


class AnalyticsConfig(AppConfig):
    name = "waldur_mastermind.analytics"
    verbose_name = "Analytics"

    def ready(self):
        from waldur_core.quotas.models import QuotaUsage
        from waldur_mastermind.marketplace import hooks as marketplace_hooks

        from . import handlers

        signals.post_save.connect(
            handlers.update_daily_quotas,
            sender=QuotaUsage,
            dispatch_uid="waldur_mastermind.analytics.handlers.update_daily_quotas",
        )
        marketplace_hooks.register_quota_history_provider(
            marketplace_hooks.QuotaHistoryProvider(
                record_daily_quotas=_record_daily_quotas,
                get_daily_quota_history=_get_daily_quota_history,
            )
        )
