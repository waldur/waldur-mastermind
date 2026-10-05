"""Reading metric data back: series for charts and figures for periods.

Figures combine in two steps. Within one series a bucket's value is the sum
for a counter and, for a gauge, the mean or last level. Series then combine
the way the offering says a project's resources combine: added up or
averaged.
"""

import collections
import datetime
import decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import timezone

from waldur_core.permissions.utils import get_scope_ids
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.managers import get_connected_offerings

from . import enums, models, retention

GAUGE_AGGREGATES = ("mean", "last", "min", "max")


def connected_service_providers(user):
    """Ids of the ServiceProvider records the user holds a role on.

    A service provider manager holds their role there rather than on the
    provider organization, so get_connected_customers misses them.
    """
    return get_scope_ids(
        user, ContentType.objects.get_for_model(marketplace_models.ServiceProvider)
    )


def consumed_offering_ids(user):
    return (
        marketplace_models.Resource.objects.exclude(state=ResourceStates.TERMINATED)
        .filter(
            Q(project__in=get_connected_projects(user))
            | Q(project__customer__in=get_connected_customers(user))
        )
        .values("offering_id")
    )


def visible_offering_metrics(user):
    qs = models.OfferingMetric.objects.all()
    if user.is_staff or user.is_support:
        return qs
    return qs.filter(
        Q(offering__customer__in=get_connected_customers(user))
        | Q(offering__customer__serviceprovider__in=connected_service_providers(user))
        | Q(offering__in=get_connected_offerings(user))
        | (
            Q(offering__in=consumed_offering_ids(user))
            & ~Q(state=enums.OfferingMetricStates.ARCHIVED)
        )
    ).distinct()


def is_provider(user, offering):
    if user.is_staff or user.is_support:
        return True
    return (
        marketplace_models.Offering.objects.filter(pk=offering.pk)
        .filter(
            Q(customer__in=get_connected_customers(user))
            | Q(customer__serviceprovider__in=connected_service_providers(user))
            | Q(id__in=get_connected_offerings(user))
        )
        .exists()
    )


def visible_series(user, offering_metric, provider=None):
    series = models.MetricSeries.objects.filter(offering_metric=offering_metric)
    if provider is None:
        provider = is_provider(user, offering_metric.offering)
    if provider:
        return series
    return series.filter(
        Q(resource__project__in=get_connected_projects(user))
        | Q(resource__project__customer__in=get_connected_customers(user))
    )


def pick_granularity(start, end, policy):
    now = timezone.now()
    span = end - start
    raw_from = now - datetime.timedelta(days=policy.raw_days) if policy else None
    hourly_from = now - datetime.timedelta(days=policy.hourly_days) if policy else None
    if span <= datetime.timedelta(days=2) and (raw_from is None or start >= raw_from):
        return enums.Granularities.RAW
    if span <= datetime.timedelta(days=62) and (
        hourly_from is None or start >= hourly_from
    ):
        return enums.Granularities.HOUR
    return enums.Granularities.DAY


def _bucket_values(series_ids, granularity, start, end, kind, aggregate):
    """{series_id: {bucket_start: value}}"""
    values = collections.defaultdict(dict)
    if granularity == enums.Granularities.RAW:
        rows = models.MetricPoint.objects.filter(
            series_id__in=series_ids, timestamp__gte=start, timestamp__lt=end
        ).values_list("series_id", "timestamp", "value")
        for series_id, timestamp, value in rows:
            values[series_id][timestamp] = value
        return values
    rows = models.MetricRollup.objects.filter(
        series_id__in=series_ids,
        granularity=granularity,
        bucket_start__gte=start,
        bucket_start__lt=end,
    ).values_list("series_id", "bucket_start", "count", "sum", "min", "max", "last")
    for series_id, bucket, count, total, low, high, last in rows:
        if kind == enums.MetricKinds.COUNTER:
            value = total
        elif aggregate == "last":
            value = last
        elif aggregate == "min":
            value = low
        elif aggregate == "max":
            value = high
        else:
            value = total / count
        values[series_id][bucket] = value
    return values


def combine(values, how):
    values = [v for v in values if v is not None]
    if not values:
        return None
    total = sum(values, decimal.Decimal(0))
    if how == enums.ProjectAggregations.MEAN:
        return total / len(values)
    return total


def series_data(
    user,
    offering_metric,
    start,
    end,
    granularity=None,
    resource=None,
    project=None,
    group_by=None,
    aggregate="mean",
):
    definition = offering_metric.definition
    series = visible_series(user, offering_metric)
    if resource is not None:
        series = series.filter(resource=resource)
    if project is not None:
        series = series.filter(resource__project=project)
    series = list(series.only("id", "attributes"))
    policy = definition.retention_policy or retention.default_policy()
    if not granularity or granularity == "auto":
        granularity = pick_granularity(start, end, policy)
    values = _bucket_values(
        [s.id for s in series], granularity, start, end, definition.kind, aggregate
    )
    groups = collections.defaultdict(list)
    for s in series:
        label = s.attributes.get(group_by) if group_by else None
        groups[label].append(s.id)
    is_gauge = definition.kind == enums.MetricKinds.GAUGE
    result = []
    for label, ids in groups.items():
        buckets = sorted({b for i in ids for b in values.get(i, {})})
        points = []
        latest = {}
        for bucket in buckets:
            if is_gauge:
                # A level holds until the next reading. Series rarely report
                # at the same instant or every hour, so each one carries its
                # latest value forward; otherwise the total drops whenever a
                # resource is quiet.
                for i in ids:
                    value = values.get(i, {}).get(bucket)
                    if value is not None:
                        latest[i] = value
                current = latest.values()
            else:
                # A missing bucket of a counter means nothing happened.
                current = [values[i].get(bucket) for i in ids if i in values]
            points.append(
                {
                    "timestamp": bucket,
                    "value": combine(current, offering_metric.project_aggregation),
                }
            )
        result.append(
            {
                "attributes": {group_by: label} if group_by else {},
                "points": points,
            }
        )
    result.sort(key=lambda g: str(g["attributes"]))
    return {"granularity": granularity, "series": result}


def period_bounds(period, now=None):
    """The current period so far and the same stretch of the previous one.

    Returns ``(start, end, previous_start, previous_end)``. A rolling window
    compares two whole windows; a calendar month or quarter compares the time
    elapsed so far with as much of the previous month or quarter.
    """
    now = now or timezone.now()
    if period == enums.GoalPeriods.ROLLING_30_DAYS:
        start = now - datetime.timedelta(days=30)
        return start, now, start - datetime.timedelta(days=30), start
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == enums.GoalPeriods.QUARTER:
        start = month_start.replace(month=(now.month - 1) // 3 * 3 + 1)
        previous_start = (start - datetime.timedelta(days=1)).replace(day=1)
        previous_start = previous_start.replace(
            month=(previous_start.month - 1) // 3 * 3 + 1
        )
    else:
        start = month_start
        previous_start = (start - datetime.timedelta(days=1)).replace(day=1)
    # Compare like with like: the previous period up to the same point, so
    # five days into a month are measured against the first five days of the
    # last one, not against all of it. Never past the previous period's end.
    previous_end = min(previous_start + (now - start), start)
    return start, now, previous_start, previous_end


def _hourly_from(policy, now=None):
    """Start of the first whole day still covered by hourly roll-ups."""
    if policy is None:
        return None
    now = now or timezone.now()
    cutoff = now - datetime.timedelta(days=policy.hourly_days)
    day = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    return day if day == cutoff else day + datetime.timedelta(days=1)


def _period_rollups(series_ids, start, end, policy):
    """Roll-ups covering [start, end): daily before the hourly cutoff, hourly after.

    Hourly roll-ups may be kept for less time than a goal period reaches back;
    reading only them would silently drop the older part of the period.
    """
    split = _hourly_from(policy)
    parts = []
    if split is not None and start < split:
        parts.append((enums.Granularities.DAY, start, min(split, end)))
        start = split
    if start < end:
        parts.append((enums.Granularities.HOUR, start, end))
    return [
        models.MetricRollup.objects.filter(
            series_id__in=series_ids,
            granularity=granularity,
            bucket_start__gte=part_start,
            bucket_start__lt=part_end,
        )
        for granularity, part_start, part_end in parts
    ]


def period_figure(offering_metric, series_ids, start, end):
    """One number for a period: a counter's total, a gauge's latest level."""
    if not series_ids:
        return None
    policy = offering_metric.definition.retention_policy or retention.default_policy()
    parts = _period_rollups(series_ids, start, end, policy)
    if offering_metric.definition.kind == enums.MetricKinds.COUNTER:
        per_series = collections.defaultdict(decimal.Decimal)
        for rollups in parts:
            for series_id, total in rollups.values_list("series_id", "sum"):
                per_series[series_id] += total
    else:
        newest = {}
        for rollups in parts:
            for series_id, bucket, last in (
                rollups.order_by("series_id", "-bucket_start")
                .distinct("series_id")
                .values_list("series_id", "bucket_start", "last")
            ):
                if series_id not in newest or bucket > newest[series_id][0]:
                    newest[series_id] = (bucket, last)
        per_series = {series_id: last for series_id, (_, last) in newest.items()}
    if not per_series:
        return None
    return combine(per_series.values(), offering_metric.project_aggregation)


# Breaks a figure down by resource rather than by an attribute; reserved as an
# attribute name, so it cannot mean both.
GROUP_BY_RESOURCE = "resource"


def visible_resource(user, uuid):
    """The resource, if the user may see its metrics: the consumer or the provider."""
    resource = (
        marketplace_models.Resource.objects.filter(uuid=uuid or None)
        .select_related("offering", "project")
        .first()
    )
    if resource is None or user.is_staff or user.is_support:
        return resource
    consumer = (
        structure_models.Project.objects.filter(pk=resource.project_id)
        .filter(
            Q(id__in=get_connected_projects(user))
            | Q(customer__in=get_connected_customers(user))
        )
        .exists()
    )
    if consumer or is_provider(user, resource.offering):
        return resource
    return None


def breakdown(user, offering_metric, group_by, start, end, project=None, resource=None):
    """A figure for a period, per value of one attribute or per resource.

    The figure is a project's, or one resource's. Each value's figure is
    computed like the whole: every series' total or latest level first, then
    combined. Taking the newest bucket of already combined series instead
    would drop the series that did not report in that bucket.
    """
    series = visible_series(user, offering_metric)
    if resource is not None:
        series = series.filter(resource=resource)
    else:
        series = series.filter(resource__project=project)
    groups = collections.defaultdict(list)
    names = {}
    for series_id, attributes, resource_uuid, resource_name in series.values_list(
        "id", "attributes", "resource__uuid", "resource__name"
    ):
        if group_by == GROUP_BY_RESOURCE:
            names[resource_uuid] = resource_name
            groups[resource_uuid].append(series_id)
        else:
            groups[attributes.get(group_by)].append(series_id)
    result = []
    for value, ids in groups.items():
        item = {"figure": period_figure(offering_metric, ids, start, end)}
        if group_by == GROUP_BY_RESOURCE:
            item.update(
                value=names[value], resource_uuid=value, resource_name=names[value]
            )
        else:
            item.update(value=value, resource_uuid=None, resource_name=None)
        result.append(item)
    result.sort(key=lambda item: -(item["figure"] or 0))
    return result


def resource_summary(user, resource):
    """Each metric the resource's offering reports, this month and last month.

    Goals apply to a project's combined figure, so a resource has none.
    """
    if resource.state == ResourceStates.TERMINATED:
        return []
    offering_metrics = (
        visible_offering_metrics(user)
        .filter(offering=resource.offering)
        .exclude(state=enums.OfferingMetricStates.ARCHIVED)
        .select_related("definition__retention_policy", "offering")
    )
    start, end, previous_start, previous_end = period_bounds(enums.GoalPeriods.MONTH)
    summary = []
    for offering_metric in offering_metrics:
        series_ids = list(
            visible_series(user, offering_metric)
            .filter(resource=resource)
            .values_list("id", flat=True)
        )
        summary.append(
            {
                "offering_metric": offering_metric,
                "period": enums.GoalPeriods.MONTH,
                "period_start": start,
                "current": period_figure(offering_metric, series_ids, start, end),
                "previous": period_figure(
                    offering_metric, series_ids, previous_start, previous_end
                ),
            }
        )
    return summary


def project_summary(user, project):
    offering_metrics = list(
        visible_offering_metrics(user)
        .filter(
            offering__in=marketplace_models.Resource.objects.filter(project=project)
            .exclude(state=ResourceStates.TERMINATED)
            .values("offering_id")
        )
        .exclude(state=enums.OfferingMetricStates.ARCHIVED)
        .select_related("definition__retention_policy", "offering")
    )
    goals = collections.defaultdict(dict)
    for goal in (
        models.MetricGoal.objects.filter(offering_metric__in=offering_metrics)
        .filter(Q(project=project) | Q(project__isnull=True))
        .select_related("project")
    ):
        goals[goal.offering_metric_id][goal.project_id] = goal
    providers = {}
    summary = []
    for offering_metric in offering_metrics:
        offering_goals = goals[offering_metric.id]
        goal = offering_goals.get(project.id) or offering_goals.get(None)
        period = goal.period if goal else enums.GoalPeriods.MONTH
        start, end, previous_start, previous_end = period_bounds(period)
        if offering_metric.offering_id not in providers:
            providers[offering_metric.offering_id] = is_provider(
                user, offering_metric.offering
            )
        series_ids = list(
            visible_series(
                user, offering_metric, providers[offering_metric.offering_id]
            )
            .filter(resource__project=project)
            .values_list("id", flat=True)
        )
        current = period_figure(offering_metric, series_ids, start, end)
        previous = period_figure(
            offering_metric, series_ids, previous_start, previous_end
        )
        summary.append(
            {
                "offering_metric": offering_metric,
                "period": period,
                "period_start": start,
                "current": current,
                "previous": previous,
                "goal": goal,
                "goal_is_project": bool(goal and goal.project_id),
                "goal_met": goal.is_met(current) if goal else None,
            }
        )
    return summary
