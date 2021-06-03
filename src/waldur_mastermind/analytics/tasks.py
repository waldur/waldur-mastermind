from celery import shared_task
from django.conf import settings as django_settings
from django.contrib.contenttypes import models as ct_models
from django.utils import timezone

from waldur_core.quotas import models as quota_models
from waldur_core.structure import models as structure_models

from . import models


@shared_task(name='analytics.sync_daily_quotas')
def sync_daily_quotas():
    date = timezone.now().date()
    for model in (structure_models.Project, structure_models.Customer):
        content_type = ct_models.ContentType.objects.get_for_model(model)
        for quota in quota_models.Quota.objects.filter(content_type=content_type):
            if not quota.scope:
                continue
            models.DailyQuotaHistory.objects.update_or_create_quota(
                quota.scope, quota.name, date, quota.usage
            )

    expiration_date = (
        timezone.now() - django_settings.WALDUR_ANALYTICS['DAILY_QUOTA_LIFETIME']
    )
    models.DailyQuotaHistory.objects.filter(date__lt=expiration_date).delete()
