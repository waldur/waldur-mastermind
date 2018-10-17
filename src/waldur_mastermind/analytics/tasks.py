from celery import shared_task

from django.contrib.contenttypes import models as ct_models
from django.utils import timezone

from waldur_core.quotas import models as quota_models
from waldur_core.structure import models as structure_models

from . import cost_tracking, openstack, slurm, utils, models


@shared_task(name='analytics.push_points')
def push_points():
    client = utils.get_influxdb_client()
    if not client:
        return
    points = []
    points.extend(openstack.get_tenants())
    points.extend(openstack.get_instances())
    points.extend(cost_tracking.get_total_cost())
    points.extend(slurm.get_usage())
    utils.write_points(client, points)


@shared_task(name='analytics.sync_daily_quotas')
def sync_daily_quotas():
    date = timezone.now().date()
    for model in (structure_models.Project, structure_models.Customer):
        content_type = ct_models.ContentType.objects.get_for_model(model)
        for quota in quota_models.Quota.objects.filter(content_type=content_type):
            if not quota.scope:
                continue
            models.DailyQuotaHistory.objects.update_or_create_quota(
                quota.scope, quota.name, date, quota.usage)
