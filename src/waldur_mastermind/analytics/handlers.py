from django.utils import timezone

from waldur_core.quotas.models import QuotaUsage
from waldur_core.structure import models as structure_models

from . import models


def update_daily_quotas(sender, instance: QuotaUsage, created=False, **kwargs):
    if not isinstance(
        instance.scope, structure_models.Project | structure_models.Customer
    ):
        return

    if not created:
        return

    # Consider dropping daily-quotas since it is actually used only by nc_user_count
    if instance.name != "nc_user_count":
        return

    models.DailyQuotaHistory.objects.update_or_create_quota(
        scope=instance.scope,
        name=instance.name,
        date=timezone.now().date(),
        usage=instance.scope.get_quota_usage(instance.name),
    )


def record_daily_quotas(entries) -> None:
    """Bulk-create DailyQuotaHistory rows from ``(scope, name, usage, date)`` tuples."""
    records = [
        models.DailyQuotaHistory(scope=scope, name=name, usage=usage, date=record_date)
        for scope, name, usage, record_date in entries
    ]
    models.DailyQuotaHistory.objects.bulk_create(records, ignore_conflicts=True)


def get_daily_quota_history(scope, name: str) -> list[dict]:
    """Return ``[{"date": ..., "usage": ...}]`` ordered by date for ``scope``."""
    return list(
        models.DailyQuotaHistory.objects.filter(scope=scope, name=name)
        .values("date", "usage")
        .order_by("date")
    )
