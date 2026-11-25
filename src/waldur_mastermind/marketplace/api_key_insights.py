"""What resource API keys spend over time: the figures and charts of the
inference Insights views (see waldur_core.core.chart_spec).

Everything is read from the keys' daily usage (ResourceApiKeyDailyUsage),
costed at each resource plan's prices as a key's estimate is
(api_key_cost). The month's figures also count what a key's month total
(``current_usages`` / ``estimated_cost``) holds beyond its daily rows, for
providers that report only monthly totals or began reporting by day mid-month;
that part shows in none of the daily charts.
"""

import datetime
import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from django.db.models import QuerySet

from waldur_core.configuration import config
from waldur_core.core import utils as core_utils
from waldur_core.core.chart_spec import (
    MAX_SERIES,
    ChartAnnotation,
    ChartSeries,
    ChartSpec,
    chart,
    fit,
    series,
)
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.marketplace import api_key_cost, models
from waldur_mastermind.marketplace.enums import BillingTypes

# Keys shown on their own in the per-key chart; the rest are summed.
TOP_KEYS = 6
# The days the recent pace is measured over.
PACE_DAYS = 7
_CENT = Decimal("0.01")
_MAX_SERIES_NAME = 100
States = models.ResourceApiKey.States


class VolumeMeter(NamedTuple):
    """One unpriced volume component distinguished by its measured unit."""

    component_type: str
    unit: str


DailyCosts = dict[tuple[int, datetime.date], Decimal]
DailyVolumes = dict[tuple[VolumeMeter, datetime.date], Decimal]
OfferingComponents = dict[int, dict[str, models.OfferingComponent]]
PricesByPlan = dict[tuple[int, int | None], api_key_cost.UnitPrices]


class DailyBreakdown(NamedTuple):
    costs: DailyCosts
    volumes: DailyVolumes


@dataclass(frozen=True)
class SpendSummary:
    spent_this_month: Decimal
    daily_pace: Decimal
    forecast_this_month: Decimal
    month_limit: Decimal | None
    runs_out: datetime.date | None


@dataclass(frozen=True)
class ApiKeyInsights:
    days: int
    spent_this_month: Decimal
    daily_pace: Decimal
    forecast_this_month: Decimal
    month_limit: Decimal | None
    runs_out: datetime.date | None
    active_keys: int
    paused_keys: int
    charts: list[ChartSpec]


def _round_cent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _key_label(api_key: models.ResourceApiKey) -> str:
    return (
        (api_key.description or "").strip() or api_key.client_id or api_key.uuid.hex[:8]
    )


def load_keys(queryset: QuerySet[models.ResourceApiKey]) -> list[models.ResourceApiKey]:
    """Evaluate a key queryset with every relation insights needs, in constant queries."""
    return list(
        queryset.select_related(None)
        .select_related("resource__offering", "resource__plan")
        .prefetch_related(
            "resource__offering__components",
            "resource__plan__components",
        )
        .defer("key_ciphertext", "usage_history")
        .order_by()
    )


def _offering_components(
    api_keys: Sequence[models.ResourceApiKey],
) -> OfferingComponents:
    by_offering: OfferingComponents = {}
    for api_key in api_keys:
        offering = api_key.resource.offering
        if offering.pk not in by_offering:
            by_offering[offering.pk] = {
                component.type: component for component in offering.components.all()
            }
    return by_offering


def _plan_prices(
    resource: models.Resource,
    prices_by_plan: PricesByPlan,
) -> api_key_cost.UnitPrices:
    plan_pk = resource.plan.pk if resource.plan is not None else None
    key = (resource.offering_id, plan_pk)
    if key not in prices_by_plan:
        prices_by_plan[key] = api_key_cost.unit_prices(resource)
    return prices_by_plan[key]


def daily(
    api_keys: Sequence[models.ResourceApiKey],
    start: datetime.date,
    end: datetime.date,
    prices_by_plan: PricesByPlan | None = None,
    offering_components: OfferingComponents | None = None,
) -> DailyBreakdown:
    """Per key and date between ``start`` and ``end``: the cost, and the
    amounts of the components the plan does not price (the volume meters,
    such as tokens)."""
    keys = {api_key.pk: api_key for api_key in api_keys}
    costs: DailyCosts = defaultdict(Decimal)
    volumes: DailyVolumes = defaultdict(Decimal)
    if not keys:
        return DailyBreakdown(costs, volumes)
    if prices_by_plan is None:
        prices_by_plan = {}
    if offering_components is None:
        offering_components = _offering_components(api_keys)
    rows = models.ResourceApiKeyDailyUsage.objects.filter(
        api_key_id__in=keys, date__gte=start, date__lte=end, usage__gt=0
    ).values_list("api_key_id", "component_type", "date", "usage")
    for api_key_id, component_type, day, usage in rows:
        api_key = keys[api_key_id]
        resource = api_key.resource
        if resource.plan is None:
            continue
        prices = _plan_prices(resource, prices_by_plan)
        if component_type in prices:
            costs[(api_key_id, day)] += api_key_cost.cost_of(
                resource, {component_type: usage}, prices
            )
        else:
            component = offering_components.get(resource.offering_id, {}).get(
                component_type
            )
            if component is not None:
                meter = VolumeMeter(component_type, component.measured_unit or "")
                volumes[(meter, day)] += usage
    return DailyBreakdown(costs, volumes)


def month_limit(
    resource: models.Resource,
    prices: api_key_cost.UnitPrices | None = None,
) -> Decimal | None:
    """The resource's own month limit as money, when every priced usage
    component has a monthly spending limit: ``sum(limit × price)``. None
    otherwise."""
    if prices is None:
        prices = api_key_cost.unit_prices(resource)
    usage_priced = {
        component_type
        for component_type, unit_price in prices.items()
        if unit_price.billing_type == BillingTypes.USAGE
    }
    if not usage_priced:
        return None
    limits = resource.spending_limits or {}
    total = Decimal(0)
    for component_type in usage_priced:
        amount = (limits.get(component_type) or {}).get("month")
        if amount is None:
            return None
        total += api_key_cost.cost_of(resource, {component_type: amount}, prices)
    return total


def without_days(
    api_keys: Sequence[models.ResourceApiKey],
    costs: DailyCosts,
    this_month: datetime.date,
    prices_by_plan: PricesByPlan | None = None,
) -> Decimal:
    """What the keys spent this month beyond their daily rows: a key's month
    total (``current_usages`` priced at the current plan, or ``estimated_cost``
    when usages are absent) counts even when its provider reported only part of
    the month by day, or none of it, so the month adds up to what the keys and
    the service limits show."""
    if prices_by_plan is None:
        prices_by_plan = {}
    by_key: dict[int, Decimal] = defaultdict(Decimal)
    for (key_id, day), cost in costs.items():
        if day >= this_month:
            by_key[key_id] += cost
    total = Decimal(0)
    for api_key in api_keys:
        if api_key.usage_period != this_month:
            continue
        if api_key.current_usages:
            resource = api_key.resource
            prices = _plan_prices(resource, prices_by_plan)
            month_cost = api_key_cost.cost_of(resource, api_key.current_usages, prices)
        else:
            month_cost = api_key.estimated_cost or Decimal(0)
        if month_cost > by_key[api_key.pk]:
            total += month_cost - by_key[api_key.pk]
    return total


def build(
    api_keys: QuerySet[models.ResourceApiKey] | Sequence[models.ResourceApiKey],
    days: int,
    resource: models.Resource | None = None,
) -> ApiKeyInsights:
    """The insights of ``api_keys`` over the last ``days`` days. With one
    ``resource`` the month chart is drawn against its own month limit."""
    keys = load_keys(api_keys) if isinstance(api_keys, QuerySet) else list(api_keys)
    # Offerings without enable_api_key_provisioning show null usage/cost in the
    # list and zero in usage_totals; keep insights consistent with them.
    managed = [api_key for api_key in keys if api_key.is_managed]
    if resource is not None and not models.ResourceApiKey.is_managed_for(resource):
        resource = None

    today = core_utils.utc_today()
    this_month = today.replace(day=1)
    window_start = today - datetime.timedelta(days=days - 1)
    pace_start = today - datetime.timedelta(days=PACE_DAYS - 1)
    start = min(window_start, this_month, pace_start)

    prices_by_plan: PricesByPlan = {}
    offering_components = _offering_components(managed)
    costs, volumes = daily(
        managed,
        start,
        today,
        prices_by_plan=prices_by_plan,
        offering_components=offering_components,
    )

    spent_by_day = sum(
        (cost for (_, day), cost in costs.items() if this_month <= day <= today),
        Decimal(0),
    )
    unreported = without_days(managed, costs, this_month, prices_by_plan=prices_by_plan)
    spent = spent_by_day + unreported
    pace = (
        sum(
            (cost for (_, day), cost in costs.items() if day >= pace_start),
            Decimal(0),
        )
        / PACE_DAYS
    )
    month_end = core_utils.month_end(today).date()
    days_left = (month_end - today).days
    forecast = spent + pace * days_left
    limit = (
        month_limit(resource, _plan_prices(resource, prices_by_plan))
        if resource
        else None
    )
    runs_out = None
    if limit is not None and spent >= limit:
        runs_out = today
    elif limit is not None and pace > 0:
        reach = today + datetime.timedelta(days=math.ceil((limit - spent) / pace))
        runs_out = reach if reach <= month_end else None

    currency = config.CURRENCY_NAME or "EUR"
    summary = SpendSummary(
        spent_this_month=quantize_price(spent),
        daily_pace=quantize_price(pace),
        forecast_this_month=quantize_price(forecast),
        month_limit=quantize_price(limit) if limit is not None else None,
        runs_out=runs_out,
    )
    return ApiKeyInsights(
        days=days,
        spent_this_month=summary.spent_this_month,
        daily_pace=summary.daily_pace,
        forecast_this_month=summary.forecast_this_month,
        month_limit=summary.month_limit,
        runs_out=summary.runs_out,
        active_keys=sum(1 for k in keys if k.state == States.OK),
        paused_keys=sum(1 for k in keys if k.state == States.PAUSED),
        charts=[
            fit(
                month_chart(
                    costs, unreported, summary, this_month, today, month_end, currency
                )
            ),
            fit(keys_chart(managed, costs, window_start, today, currency)),
            fit(volume_chart(volumes, offering_components, window_start, today)),
        ],
    )


def month_chart(
    costs: DailyCosts,
    unreported: Decimal,
    summary: SpendSummary,
    this_month: datetime.date,
    today: datetime.date,
    month_end: datetime.date,
    currency: str,
) -> ChartSpec:
    """Spend so far this month, its forecast to the month's end and, for one
    resource, its month limit and the day it is reached at the recent pace.
    Spend reported for the month but not by day starts the line, so it ends
    on the month's total."""
    by_day: dict[datetime.date, Decimal] = defaultdict(Decimal)
    for (_, day), cost in costs.items():
        if day >= this_month and cost > 0:
            by_day[day] += cost
    running, points = unreported, []
    for day in core_utils.date_range(this_month, today):
        running += by_day.get(day, Decimal(0))
        points.append([core_utils.date_to_utc_ms(day), float(quantize_price(running))])
    items: list[ChartSeries] = (
        [series("Spent", points, tone="info")] if by_day or unreported else []
    )
    if items and summary.daily_pace and today < month_end:
        items.append(
            series(
                "Forecast",
                [
                    points[-1],
                    [
                        core_utils.date_to_utc_ms(month_end),
                        float(summary.forecast_this_month),
                    ],
                ],
                style="dashed",
                tone="warning" if summary.runs_out else "neutral",
            )
        )
    annotations: list[ChartAnnotation] = [
        {"type": "line_x", "value": core_utils.date_to_utc_ms(today), "label": "Today"}
    ]
    if summary.month_limit is not None:
        annotations.append(
            {
                "type": "line_y",
                "value": float(summary.month_limit),
                "label": f"Month limit {summary.month_limit:,.2f} {currency}",
                "tone": "danger",
            }
        )
    if summary.runs_out:
        if summary.runs_out == today:
            annotations[0] = {
                "type": "line_x",
                "value": core_utils.date_to_utc_ms(today),
                "label": "Today · Limit reached",
                "tone": "warning",
            }
        else:
            annotations.append(
                {
                    "type": "line_x",
                    "value": core_utils.date_to_utc_ms(summary.runs_out),
                    "label": f"Limit reached {summary.runs_out:%d %b}",
                    "tone": "warning",
                }
            )
    subtitle = f"{summary.spent_this_month:,.2f} {currency} so far"
    if summary.daily_pace and today < month_end:
        subtitle += (
            f"; about {summary.forecast_this_month:,.2f} by "
            f"{month_end:%d %b} at the recent pace"
        )
    if unreported:
        subtitle += (
            f". {quantize_price(unreported):,.2f} {currency} of it was "
            "reported for the month, not by day"
        )
    return chart(
        "area",
        "Spend this month",
        x={"type": "time"},
        y={"type": "value", "label": currency, "min": 0},
        series=items,
        subtitle=subtitle,
        annotations=annotations,
        empty_message="No usage has been reported this month yet.",
    )


def _fit_label(base: str, suffix: str | None = None) -> str:
    if suffix is None:
        if len(base) <= _MAX_SERIES_NAME:
            return base
        return base[: _MAX_SERIES_NAME - 1] + "…"
    tag = f" ({suffix})"
    if len(tag) >= _MAX_SERIES_NAME:
        return suffix[: _MAX_SERIES_NAME - 1] + "…"
    room = _MAX_SERIES_NAME - len(tag)
    prefix = base if len(base) <= room else base[: max(room - 1, 1)] + "…"
    return f"{prefix}{tag}"


def _unique_key_labels(
    top: list[int],
    keys_by_id: dict[int, models.ResourceApiKey],
    has_other: bool,
) -> dict[int, str]:
    raw = {key_id: _key_label(keys_by_id[key_id]) for key_id in top}
    # Fit before counting so descriptions that collide only after truncation are caught.
    fitted = {key_id: _fit_label(name) for key_id, name in raw.items()}
    counts = Counter(fitted.values())
    if has_other:
        counts["Other keys"] += 1
    labels = {}
    for key_id in top:
        if counts[fitted[key_id]] == 1:
            labels[key_id] = fitted[key_id]
        else:
            api_key = keys_by_id[key_id]
            suffix = api_key.client_id or api_key.uuid.hex[:8]
            labels[key_id] = _fit_label(raw[key_id], suffix)
    resolved_counts = Counter(labels.values())
    for key_id in top:
        if resolved_counts[labels[key_id]] > 1:
            labels[key_id] = _fit_label(raw[key_id], keys_by_id[key_id].uuid.hex[:8])
    return labels


def keys_chart(
    api_keys: Sequence[models.ResourceApiKey],
    costs: DailyCosts,
    start: datetime.date,
    end: datetime.date,
    currency: str,
) -> ChartSpec:
    """Spend per day, stacked by key: the biggest spenders on their own, the
    rest together."""
    by_key: dict[int, Decimal] = defaultdict(Decimal)
    for (key_id, day), cost in costs.items():
        if start <= day <= end and cost > 0:
            by_key[key_id] += cost
    by_key = {key_id: total for key_id, total in by_key.items() if total > 0}
    keys_by_id = {api_key.pk: api_key for api_key in api_keys}
    raw_names = {key_id: _key_label(api_key) for key_id, api_key in keys_by_id.items()}
    ranked = sorted(
        by_key, key=lambda key_id: (-by_key[key_id], raw_names[key_id], key_id)
    )
    top, rest = ranked[:TOP_KEYS], set(ranked[TOP_KEYS:])
    names = _unique_key_labels(top, keys_by_id, bool(rest))
    days = core_utils.date_range(start, end)
    items: list[ChartSeries] = []
    for key_id in top:
        points = [
            [
                core_utils.date_to_utc_ms(day),
                float(_round_cent(costs.get((key_id, day), Decimal(0)))),
            ]
            for day in days
        ]
        if any(y > 0 for _, y in points):
            items.append(series(names[key_id], points))
    if rest:
        other_points = [
            [
                core_utils.date_to_utc_ms(day),
                float(
                    _round_cent(
                        sum(
                            (costs.get((key_id, day), Decimal(0)) for key_id in rest),
                            Decimal(0),
                        )
                    )
                ),
            ]
            for day in days
        ]
        if any(y > 0 for _, y in other_points):
            items.append(series("Other keys", other_points, tone="neutral"))
    return chart(
        "stacked_bar",
        "Spend per day by key",
        x={"type": "time"},
        y={"type": "value", "label": currency, "min": 0},
        series=items,
        subtitle=f"Last {len(days)} days",
        empty_message="No key has spent anything in this period.",
    )


def volume_chart(
    volumes: DailyVolumes,
    offering_components: OfferingComponents,
    start: datetime.date,
    end: datetime.date,
) -> ChartSpec:
    """The volume meters per day, such as input and output tokens."""
    labels: dict[VolumeMeter, str] = {}
    for components in offering_components.values():
        for component in components.values():
            meter = VolumeMeter(component.type, component.measured_unit or "")
            labels.setdefault(meter, component.name or component.type)
    totals: dict[VolumeMeter, Decimal] = defaultdict(Decimal)
    for (meter, day), amount in volumes.items():
        if start <= day <= end and amount > 0:
            totals[meter] += amount
    top_meters = sorted(sorted(totals, key=lambda m: (-totals[m], m))[:MAX_SERIES])
    by_type = Counter(meter.component_type for meter in top_meters)
    units = {meter.unit for meter in top_meters if meter.unit}
    days = core_utils.date_range(start, end)
    items: list[ChartSeries] = []
    for meter in top_meters:
        name = labels.get(meter, meter.component_type)
        if by_type[meter.component_type] > 1 and meter.unit:
            name = f"{name} ({meter.unit})"
        items.append(
            series(
                name,
                [
                    [
                        core_utils.date_to_utc_ms(day),
                        float(volumes.get((meter, day), 0)),
                    ]
                    for day in days
                ],
            )
        )
    return chart(
        "line",
        "Usage per day",
        x={"type": "time"},
        y={
            "type": "value",
            "label": units.pop() if len(units) == 1 else "Amount",
            "min": 0,
        },
        series=items,
        subtitle=f"Last {len(days)} days",
        empty_message="No usage has been reported in this period.",
    )
