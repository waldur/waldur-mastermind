"""One way in for custom metric points, whichever protocol carried them.

Authorization is all or nothing: a caller who may not report for one of the
resources in a request gets 403 and nothing is written, so a refusal never
tells an outsider anything about a resource. Everything else is decided point
by point and refused points come back with a reason, the way OTLP reports
partial success, so one bad point does not cost a service its whole batch.
"""

import dataclasses
import datetime
import decimal
import hashlib
import json
import math
import uuid as uuid_lib

from constance import config
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission_on_any_source
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import ResourceStates

from . import enums, models

# A clock slightly ahead is normal; one reporting next week is a bug, and its
# points would win every "latest value" until then.
MAX_CLOCK_SKEW = datetime.timedelta(hours=1)
MAX_ATTRIBUTE_VALUE_LENGTH = 256
MAX_VALUE = decimal.Decimal(10) ** 18
QUANTUM = decimal.Decimal("0.000001")
REPORTABLE_STATES = (
    ResourceStates.OK,
    ResourceStates.UPDATING,
    ResourceStates.TERMINATING,
)

# What the sender says a point is. The native endpoint leaves it to the
# definition; OTLP states it, and a mismatch is refused rather than guessed.
GAUGE = "gauge"
COUNTER = "counter"


@dataclasses.dataclass
class Point:
    resource_uuid: str
    metric: str
    timestamp: datetime.datetime
    value: decimal.Decimal
    attributes: dict = dataclasses.field(default_factory=dict)
    # Present for a cumulative counter: when the running total started.
    start_time: datetime.datetime | None = None
    reported_kind: str | None = None


@dataclasses.dataclass
class Result:
    accepted: int = 0
    rejected: list = dataclasses.field(default_factory=list)

    def reject(self, index, reason):
        self.rejected.append({"index": index, "reason": str(reason)})


class Rejected(Exception):
    pass


# Where a provider-side role can be held: on the offering (offering manager),
# its organization (owner) or the organization's ServiceProvider record
# (service provider manager).
OFFERING_PROVIDER_SOURCES = ["*", "customer", "customer.serviceprovider"]


def can_report(request, resource):
    return has_permission_on_any_source(
        request,
        PermissionEnum.SET_RESOURCE_USAGE,
        resource.offering,
        OFFERING_PROVIDER_SOURCES,
    )


def attributes_hash(attributes):
    return hashlib.sha256(
        json.dumps(attributes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize_uuid(value):
    """Any valid UUID spelling, as the hex the database keys on; None if invalid."""
    try:
        return uuid_lib.UUID(str(value)).hex
    except (ValueError, AttributeError, TypeError):
        return None


def _resolve_resources(request, points):
    uuids = {normalize_uuid(p.resource_uuid) for p in points}
    if None in uuids:
        # Unknown and forbidden look the same to the caller.
        raise PermissionDenied()
    resources = {
        r.uuid.hex: r
        for r in marketplace_models.Resource.objects.filter(
            uuid__in=uuids
        ).select_related("offering", "offering__customer")
    }
    for uuid in uuids:
        resource = resources.get(uuid)
        if resource is None or not can_report(request, resource):
            raise PermissionDenied()
    return resources


class _Context:
    def __init__(self):
        self.metrics_by_offering = {}
        self.series = {}
        self.series_of = {}

    def offering_metric(self, offering, key):
        if offering.id not in self.metrics_by_offering:
            self.metrics_by_offering[offering.id] = {
                om.definition.key: om
                for om in models.OfferingMetric.objects.filter(
                    offering=offering
                ).select_related("definition")
            }
        return self.metrics_by_offering[offering.id].get(key)

    def existing_series(self, resource, offering_metric):
        cache_key = (resource.id, offering_metric.id)
        if cache_key not in self.series_of:
            self.series_of[cache_key] = list(
                models.MetricSeries.objects.filter(
                    resource=resource, offering_metric=offering_metric
                )
            )
        return self.series_of[cache_key]


def _validate_value(value):
    try:
        value = decimal.Decimal(value)
    except (decimal.InvalidOperation, TypeError, ValueError):
        raise Rejected("Value is not a number.")
    if not value.is_finite() or abs(value) >= MAX_VALUE:
        raise Rejected("Value is out of range.")
    return value.quantize(QUANTUM, rounding=decimal.ROUND_HALF_EVEN)


def _validate_timestamp(timestamp, now):
    if timestamp > now + MAX_CLOCK_SKEW:
        raise Rejected("Timestamp is in the future.")
    if timestamp < now - datetime.timedelta(days=config.METRICS_LATE_DATA_DAYS):
        raise Rejected("Timestamp is older than the late-data window.")


def _validate_attributes(attributes, definition):
    if not isinstance(attributes, dict):
        raise Rejected("Attributes must be a map of names to values.")
    allowed = set(definition.attribute_keys or [])
    for key, value in attributes.items():
        if key not in allowed:
            raise Rejected(
                f"Attribute {key} is not declared by metric {definition.key}."
            )
        if isinstance(value, bool) or isinstance(value, int):
            continue
        if isinstance(value, float):
            # Postgres jsonb has no NaN or Infinity; storing one would fail
            # the whole batch instead of this point.
            if not math.isfinite(value):
                raise Rejected(f"Attribute {key} must be a finite number.")
            continue
        if not isinstance(value, str):
            raise Rejected(f"Attribute {key} must be a text, number or boolean.")
        if len(value) > MAX_ATTRIBUTE_VALUE_LENGTH:
            raise Rejected(f"Attribute {key} is too long.")
        if "\x00" in value:
            raise Rejected(f"Attribute {key} contains a NUL character.")


def _offering_metric(context, resource, point):
    offering_metric = context.offering_metric(resource.offering, point.metric)
    if offering_metric is None:
        raise Rejected(
            f"Metric {point.metric} is not adopted by the resource's offering."
        )
    if offering_metric.state == enums.OfferingMetricStates.ARCHIVED:
        raise Rejected(f"Metric {point.metric} is archived.")
    if offering_metric.state == enums.OfferingMetricStates.PAUSED:
        raise Rejected(f"Metric {point.metric} is paused.")
    kind = offering_metric.definition.kind
    if point.reported_kind and point.reported_kind != kind:
        raise Rejected(
            f"Metric {point.metric} is a {kind}, not a {point.reported_kind}."
        )
    if point.start_time is not None and kind != enums.MetricKinds.COUNTER:
        raise Rejected("Only a counter can be reported as a running total.")
    return offering_metric


def _series(context, resource, offering_metric, attributes):
    digest = attributes_hash(attributes)
    cache_key = (resource.id, offering_metric.id, digest)
    if cache_key in context.series:
        return context.series[cache_key]
    existing = context.existing_series(resource, offering_metric)
    for series in existing:
        if series.attributes_hash == digest:
            context.series[cache_key] = series
            return series
    if len(existing) >= config.METRICS_MAX_SERIES_PER_RESOURCE_METRIC:
        raise Rejected("Too many attribute combinations for this metric and resource.")
    limit = offering_metric.definition.max_attribute_values
    for key, value in attributes.items():
        values = {s.attributes.get(key) for s in existing if key in s.attributes}
        if value not in values and len(values) >= limit:
            raise Rejected(f"Attribute {key} has too many distinct values.")
    series, _created = models.MetricSeries.objects.get_or_create(
        resource=resource,
        offering_metric=offering_metric,
        attributes_hash=digest,
        defaults={"attributes": attributes},
    )
    existing.append(series)
    context.series[cache_key] = series
    return series


def _check_stateless(offering_metric, point, value):
    """Refusals that need no counter state, made before any series exists."""
    if offering_metric.definition.kind != enums.MetricKinds.COUNTER:
        return
    if point.start_time is None and value < 0:
        raise Rejected("A counter cannot decrease.")
    if point.start_time is not None and point.start_time > point.timestamp:
        raise Rejected("A running total cannot start after it was measured.")


def _counter_increment(series, point, value, now):
    """Turn a counter point into the increment it stands for.

    A delta point already is one. A running total is compared with the last
    one: a new start time or a smaller total means the counter restarted, and
    the total counts from zero again.

    The first total of a series is credited in full only when the counter
    started inside the late-data window. A counter that has been running for
    longer, say a service adopting a metric mid-life, is taken as a baseline:
    the points before it were never reported, so crediting them all at once
    would put months of activity into one moment.
    """
    if point.start_time is None:
        return value
    if series.last_timestamp is not None and point.timestamp <= series.last_timestamp:
        if point.start_time == series.start_time and value == series.last_value:
            return None  # a retry of the last total: nothing new
        raise Rejected("Running total is older than the last one reported.")
    first = series.last_value is None
    restarted = (
        first or series.start_time != point.start_time or value < series.last_value
    )
    if not restarted:
        increment = value - series.last_value
    elif first and point.start_time < now - datetime.timedelta(
        days=config.METRICS_LATE_DATA_DAYS
    ):
        increment = None
    else:
        increment = value
    series.start_time = point.start_time
    series.last_value = value
    series.last_timestamp = point.timestamp
    series._counter_state_changed = True
    return increment


def _lock_running_totals(entries):
    """Lock every series that carries a running total, in one go, by pk.

    Locking them one by one in the order points arrive lets two requests take
    the same two series in opposite order and deadlock.
    """
    by_id = {
        series.id: series
        for _, point, series, _, _ in entries
        if point.start_time is not None
    }
    if not by_id:
        return
    for locked in (
        models.MetricSeries.objects.select_for_update()
        .filter(pk__in=sorted(by_id))
        .order_by("pk")
    ):
        series = by_id[locked.pk]
        series.start_time = locked.start_time
        series.last_value = locked.last_value
        series.last_timestamp = locked.last_timestamp


def ingest(request, points):
    if len(points) > config.METRICS_MAX_POINTS_PER_REQUEST:
        raise Rejected(
            f"At most {config.METRICS_MAX_POINTS_PER_REQUEST} points per request."
        )
    result = Result()
    if not points:
        return result
    resources = _resolve_resources(request, points)
    now = timezone.now()
    context = _Context()
    rows = {}

    # Running totals have to be applied in time order per series.
    order = sorted(range(len(points)), key=lambda i: points[i].timestamp)
    with transaction.atomic():
        # Pass 1: everything that needs no counter state. A series is only
        # created for a point that got this far.
        entries = []
        for index in order:
            point = points[index]
            try:
                resource = resources[normalize_uuid(point.resource_uuid)]
                if resource.state not in REPORTABLE_STATES:
                    raise Rejected("Resource is not in a state that accepts metrics.")
                offering_metric = _offering_metric(context, resource, point)
                _validate_timestamp(point.timestamp, now)
                value = _validate_value(point.value)
                _validate_attributes(point.attributes, offering_metric.definition)
                _check_stateless(offering_metric, point, value)
                series = _series(context, resource, offering_metric, point.attributes)
            except Rejected as e:
                result.reject(index, e)
                continue
            entries.append((index, point, series, value, offering_metric))

        _lock_running_totals(entries)

        # Pass 2: counters, in time order, against the locked state.
        for index, point, series, value, offering_metric in entries:
            try:
                if offering_metric.definition.kind == enums.MetricKinds.COUNTER:
                    value = _counter_increment(series, point, value, now)
            except Rejected as e:
                result.reject(index, e)
                continue
            result.accepted += 1
            if value is not None:
                # The same series and timestamp twice in one batch: last wins,
                # as a correction sent later would.
                rows[(series.id, point.timestamp)] = models.MetricPoint(
                    series=series, timestamp=point.timestamp, value=value
                )
        models.MetricPoint.objects.bulk_create(
            rows.values(),
            update_conflicts=True,
            unique_fields=["series", "timestamp"],
            update_fields=["value"],
        )
        changed = [
            s
            for s in context.series.values()
            if getattr(s, "_counter_state_changed", False)
        ]
        if changed:
            models.MetricSeries.objects.bulk_update(
                changed, ["start_time", "last_value", "last_timestamp"]
            )
    result.rejected.sort(key=lambda r: r["index"])
    return result
