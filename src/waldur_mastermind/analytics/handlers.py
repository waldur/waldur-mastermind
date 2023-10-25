from django.utils import timezone

from waldur_core.structure import models as structure_models

from . import models


def update_daily_quotas(sender, instance, created=False, **kwargs):
    if not isinstance(
        instance.scope, (structure_models.Project, structure_models.Customer)
    ):
        return

    if not created:
        return

    models.DailyQuotaHistory.objects.update_or_create_quota(
        scope=instance.scope,
        name=instance.name,
        date=timezone.now().date(),
        usage=instance.scope.get_quota_usage(instance.name),
    )
