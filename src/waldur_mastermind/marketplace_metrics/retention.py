"""Deleting metric data once its retention policy says it is no longer kept."""

import datetime
import logging

from constance import config
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from . import enums, models

logger = logging.getLogger(__name__)

BATCH_SIZE = 10000


def default_policy():
    """The policy for definitions that name none.

    If it is missing, an unsaved policy with the model's defaults stands in,
    so those definitions are still trimmed instead of kept forever.
    """
    name = config.METRICS_DEFAULT_RETENTION_POLICY
    policy = models.RetentionPolicy.objects.filter(name=name).first()
    if policy is None:
        logger.warning(
            "Default metric retention policy %r is missing; using the built-in "
            "defaults until it is restored.",
            name,
        )
        policy = models.RetentionPolicy(name=name)
    return policy


def _scopes():
    """(policy, series ids) pairs covering every series exactly once."""
    fallback = default_policy()
    scopes = []
    for policy in models.RetentionPolicy.objects.all():
        condition = Q(offering_metric__definition__retention_policy=policy)
        if policy.pk == fallback.pk:
            condition |= Q(offering_metric__definition__retention_policy__isnull=True)
        scopes.append((policy, models.MetricSeries.objects.filter(condition)))
    if fallback.pk is None:
        scopes.append(
            (
                fallback,
                models.MetricSeries.objects.filter(
                    offering_metric__definition__retention_policy__isnull=True
                ),
            )
        )
    return [(policy, series.values("id")) for policy, series in scopes]


def _delete_in_batches(queryset):
    deleted = 0
    while True:
        ids = list(queryset.values_list("id", flat=True)[:BATCH_SIZE])
        if not ids:
            return deleted
        deleted += queryset.model.objects.filter(id__in=ids).delete()[0]


def enforce():
    now = timezone.now()
    for policy, series in _scopes():
        cutoffs = (
            (models.MetricPoint.objects, "timestamp", policy.raw_days),
            (
                models.MetricRollup.objects.filter(
                    granularity=enums.Granularities.HOUR
                ),
                "bucket_start",
                policy.hourly_days,
            ),
            (
                models.MetricRollup.objects.filter(granularity=enums.Granularities.DAY),
                "bucket_start",
                policy.daily_days,
            ),
        )
        for queryset, field, days in cutoffs:
            if days is None:
                continue
            cutoff = now - datetime.timedelta(days=days)
            deleted = _delete_in_batches(
                queryset.filter(series_id__in=series, **{f"{field}__lt": cutoff})
            )
            if deleted:
                logger.info(
                    "Deleted %s %s rows older than %s under retention policy %s.",
                    deleted,
                    queryset.model.__name__,
                    cutoff,
                    policy.name,
                )
    drop_empty_series(now)


def drop_empty_series(now=None):
    """Series whose data has all expired. A day's grace spares new ones."""
    now = now or timezone.now()
    empty = (
        models.MetricSeries.objects.filter(created__lt=now - datetime.timedelta(days=1))
        .exclude(Exists(models.MetricPoint.objects.filter(series=OuterRef("pk"))))
        .exclude(Exists(models.MetricRollup.objects.filter(series=OuterRef("pk"))))
    )
    return _delete_in_batches(empty)


def purge(offering_metric_id):
    """Delete everything an archived offering metric collected, then the metric.

    The state is checked again under a lock: the metric may have been resumed
    between the request to purge it and this running.
    """
    with transaction.atomic():
        offering_metric = (
            models.OfferingMetric.objects.select_for_update()
            .filter(pk=offering_metric_id, state=enums.OfferingMetricStates.ARCHIVED)
            .first()
        )
        if offering_metric is None:
            return False
        for series_id in offering_metric.series.values_list("id", flat=True):
            _delete_in_batches(models.MetricPoint.objects.filter(series_id=series_id))
            _delete_in_batches(models.MetricRollup.objects.filter(series_id=series_id))
        offering_metric.series.all().delete()
        offering_metric.delete()
    return True


def purge_archived():
    cutoff = timezone.now() - datetime.timedelta(days=config.METRICS_ARCHIVE_GRACE_DAYS)
    for offering_metric_id in models.OfferingMetric.objects.filter(
        state=enums.OfferingMetricStates.ARCHIVED, modified__lt=cutoff
    ).values_list("id", flat=True):
        purge(offering_metric_id)
