from celery import shared_task
from django.conf import settings as django_settings
from django.utils import timezone

from waldur_core.structure import models as structure_models

from . import models


@shared_task(name="analytics.sync_daily_quotas")
def sync_daily_quotas():
    date = timezone.now().date()
    for model in (structure_models.Project, structure_models.Customer):
        for scope in model.objects.all():
            for name, value in scope.quota_usages.items():
                models.DailyQuotaHistory.objects.update_or_create_quota(
                    scope, name, date, value
                )

    expiration_date = (
        timezone.now() - django_settings.WALDUR_ANALYTICS["DAILY_QUOTA_LIFETIME"]
    )
    models.DailyQuotaHistory.objects.filter(date__lt=expiration_date).delete()
