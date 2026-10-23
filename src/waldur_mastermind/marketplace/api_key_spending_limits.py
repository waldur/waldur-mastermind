"""Spending limits of a resource API key, per component and calendar period.

A key's limits have the shape of its resource's spending limits
(``{component_type: {period: amount}}``, see ``spending_limits``) and the same
periods, reset times and ordering rule. Two offering options decide which
periods a key is offered (``offered_periods``). ``api_key_limits``, on unless
set to false, lets consumers limit keys at all. ``api_key_daily_usage``, off
unless set, is the provider's declaration that the site agent reports each
key's usage per day: without it only ``month`` is offered, as day and week
limits would never be reached and quarter and year would count the current
month alone. With it, a key is offered ``month`` on every component, as key
limits were monthly before they had periods, and the other periods where the
component offers them to resources. ``total`` is offered where the component
offers it, with or without daily usage: it needs no daily rows. A component
the resource's plan prices
at 0 takes limits like any other: the price says how Waldur invoices it, not
whether anything is spent on it, and a LiteLLM cost cap is priced at 0.
A key's limit for a period may not exceed the resource's own for that period
or for any longer one, as the key could never spend more than the resource,
and must be above zero: in the flat ``limits`` shape agents and older clients
still read, zero means no limit, so a zero limit would block in Waldur and read
as none everywhere else. A key is stopped with a pause instead.

The site agent reports a key's usage per month (``current_usages`` for
``usage_period``), per day (``ResourceApiKeyDailyUsage``), or both. The month
figure of a component that has daily amounts is their sum, so month limits
read one number however the usage was reported. Day and week sum the daily
rows; quarter and year add the month figure to the daily rows of the earlier
months in the period, so a key reported only monthly counts its current month
there and no earlier one. ``total`` adds the month figure to ``usage_history``,
where each month's figure is kept when the next month's usage comes in
(``close_month``), so it counts every month the key reported in, monthly or
daily, however long ago. A month figure never drops: a report for a month,
current or closed, raises its figure and never lowers it, so a month total
sent late for a closed month (``raise_closed_month``) or a day that comes in
late for one (``count_late_days``) raises that month's figure as it would the
current one's. A total never resets: a key it pauses has no
``limit_resumes_at`` and resumes only when the limit is raised or cleared.

Enforcement is the key's own: ``resource_api_keys.record_usage`` pauses the key
alone once a limit is reached, records in ``limit_resumes_at`` when every reached
limit resets, and ``resource_api_keys.resume_keys_under_limit`` resumes it then.
"""

import datetime
from decimal import ROUND_UP, Decimal, InvalidOperation

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import BooleanField, Exists, F, Func, OuterRef, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_mastermind.marketplace import models, spending_limits
from waldur_mastermind.marketplace.enums import SpendingLimitPeriods

PERIODS = SpendingLimitPeriods.ORDER
MONTH = SpendingLimitPeriods.MONTH
YEAR = SpendingLimitPeriods.YEAR
TOTAL = SpendingLimitPeriods.TOTAL
# The periods a key's month figure answers on its own, or with its usage
# history: neither depends on the agent reporting per day.
WITHOUT_DAILY_USAGE = (MONTH, TOTAL)


LIMITS_OPTION = "api_key_limits"
DAILY_USAGE_OPTION = "api_key_daily_usage"


def limits_offered(offering) -> bool:
    """Whether consumers may set limits on the offering's keys; on unless turned off."""
    return (offering.plugin_options or {}).get(LIMITS_OPTION) is not False


def daily_usage_reported(offering) -> bool:
    """Whether the provider declares that each key's usage is reported per day."""
    return (offering.plugin_options or {}).get(DAILY_USAGE_OPTION) is True


def offered_periods(resource) -> dict[str, set[str]]:
    """The periods a key of ``resource`` may limit, per component type.

    Every component of the offering is listed, with no period at all where
    limits are off for the offering.
    """
    offering = resource.offering
    types = list(offering.components.values_list("type", flat=True))
    if not limits_offered(offering):
        return {component_type: set() for component_type in types}
    capped = spending_limits.capped_components(offering, resource.plan)
    daily = daily_usage_reported(offering)

    def periods(component_type):
        component = capped.get(component_type)
        offered = set(component.spending_limit_periods) if component else set()
        return {MONTH} | (offered if daily else offered & {TOTAL})

    return {component_type: periods(component_type) for component_type in types}


def _not_offered(resource, component_type, period, offered) -> str:
    """Why a ``period`` limit is refused on ``component_type``."""
    if not limits_offered(resource.offering):
        return _("The offering does not offer limits on API keys.")
    message = _("A %(period)s limit is not offered. Offered: %(offered)s.") % {
        "period": period,
        "offered": ", ".join(p for p in PERIODS if p in offered),
    }
    if period not in WITHOUT_DAILY_USAGE and not daily_usage_reported(
        resource.offering
    ):
        message = "{} {}".format(
            message,
            _("Other periods need the provider to report each key's usage per day."),
        )
    return message


def with_monthly_limits(current: dict | None, monthly: dict | None) -> dict:
    """``current`` with its month limits replaced by ``monthly``.

    ``monthly`` is the flat ``{component_type: amount}`` shape key limits had
    before they had periods, where zero means no limit. The other periods are
    kept. Its amounts are rounded up to the cent, since that shape always took
    any number; one the key holds already is kept as it is.
    """
    result = {
        component_type: {
            period: amount for period, amount in periods.items() if period != MONTH
        }
        for component_type, periods in (current or {}).items()
    }
    for component_type, amount in (monthly or {}).items():
        if amount:
            held = ((current or {}).get(component_type) or {}).get(MONTH)
            result.setdefault(component_type, {})[MONTH] = _to_cents(amount, held)
    return {
        component_type: periods for component_type, periods in result.items() if periods
    }


def _to_cents(raw, held):
    """``raw`` rounded up to the cent, unless it is ``held`` or not a number.

    Rounding up keeps a positive limit above 0, and against usage reported in
    cents it is reached at the same amount as the unrounded one. Anything that
    is not a number is left for the validation to refuse.
    """
    if (
        _is_held(raw, held)
        or isinstance(raw, bool)
        or not isinstance(raw, int | float | str)
    ):
        return raw
    try:
        amount = Decimal(str(raw).strip())
    except InvalidOperation:
        return raw
    if not amount.is_finite() or abs(amount) >= spending_limits.MAX_AMOUNT:
        return raw
    return spending_limits.json_number(
        amount.quantize(
            Decimal(1).scaleb(-spending_limits.DECIMAL_PLACES), rounding=ROUND_UP
        )
    )


def _is_held(raw, held) -> bool:
    """Whether ``raw`` is the amount ``held``, written in any number form."""
    if held is None or isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return False
    try:
        return Decimal(str(raw).strip()) == Decimal(str(held))
    except InvalidOperation:
        return False


def resource_cap(resource_limits: dict, period: str):
    """The resource's limit that caps a key's ``period`` limit, with its period.

    The lowest of the resource's limits for ``period`` and the longer periods,
    or None. Of equal ones, the shortest period's is named.
    """
    caps = [
        (Decimal(str(resource_limits[longer])), longer)
        for longer in PERIODS[PERIODS.index(period) :]
        if resource_limits.get(longer) is not None
    ]
    return min(caps, key=lambda cap: cap[0], default=None)


def validate_key_limits(value, resource, current: dict | None = None) -> dict | None:
    """Clean the spending limits set on a key of ``resource``.

    A null amount clears that period's limit. An amount the key already holds
    is kept as it is, even if its period is no longer offered or the resource's
    limit has since dropped below it, so that editing one period never forces
    a change to another; the ordering rule still applies to all of them.
    """
    if not value:
        return None
    if not isinstance(value, dict):
        raise serializers.ValidationError(
            _("Spending limits must be an object keyed by component type.")
        )
    offered = offered_periods(resource)
    resource_limits = resource.spending_limits or {}
    current = current or {}
    cleaned = {}
    for component_type, periods in value.items():
        if periods is None or (
            isinstance(periods, dict) and all(v is None for v in periods.values())
        ):
            continue
        if component_type not in offered:
            raise serializers.ValidationError(
                {component_type: _("The offering has no such component.")}
            )
        if not isinstance(periods, dict):
            raise serializers.ValidationError(
                {
                    component_type: _(
                        "Spending limits must be an object keyed by period."
                    )
                }
            )
        held = current.get(component_type) or {}
        amounts = {}
        for period, raw in periods.items():
            if raw is None:
                continue
            if period not in PERIODS:
                raise serializers.ValidationError(
                    {
                        component_type: _(
                            "Unknown period %(period)s. Choose from %(periods)s."
                        )
                        % {"period": period, "periods": ", ".join(PERIODS)}
                    }
                )
            # A held amount sent back as it is passes even the precision rule:
            # a limit set before key limits had periods may have more decimals.
            unchanged = _is_held(raw, held.get(period))
            amount = (
                Decimal(str(held[period]))
                if unchanged
                else spending_limits.parse_amount(raw, _("The %s limit") % period)
            )
            if not amount:
                raise serializers.ValidationError(
                    {
                        component_type: _(
                            "The %s limit must be above 0; pause the key to stop it."
                        )
                        % period
                    }
                )
            if not unchanged:
                if period not in offered[component_type]:
                    raise serializers.ValidationError(
                        {
                            component_type: _not_offered(
                                resource,
                                component_type,
                                period,
                                offered[component_type],
                            )
                        }
                    )
                cap = resource_cap(resource_limits.get(component_type) or {}, period)
                if cap is not None and amount > cap[0]:
                    raise serializers.ValidationError(
                        {
                            component_type: _(
                                "The %(period)s limit may not exceed the "
                                "resource's %(cap_period)s spending limit of "
                                "%(cap)s."
                            )
                            % {
                                "period": period,
                                "cap_period": cap[1],
                                "cap": spending_limits.json_number(cap[0]),
                            }
                        }
                    )
            amounts[period] = amount
        try:
            spending_limits.validate_order(amounts, _("Limits"))
        except serializers.ValidationError as error:
            raise serializers.ValidationError({component_type: error.detail})
        if amounts:
            cleaned[component_type] = {
                period: spending_limits.json_number(amounts[period])
                for period in PERIODS
                if period in amounts
            }
    return cleaned or None


# The limits a request asked for, as sent: ``(field, value)``, where field is
# ``limits`` (the flat monthly shape) or ``spending_limits``. Validation runs
# unlocked; the command validates them again with the key and the resource
# locked, against what they hold then.
REQUESTED = "requested_spending_limits"


def clean_requested_limits(requested, resource, current: dict | None) -> dict | None:
    """The key's new spending limits from a request's ``(field, value)``.

    ``current`` is what the key holds, which the flat monthly shape keeps the
    other periods of, and which may be sent back as it is.
    """
    field, value = requested
    if field == "limits":
        value = with_monthly_limits(current, value)
    return validate_key_limits(value, resource, current)


# --- usage --------------------------------------------------------------------


def _month_end(month: datetime.date) -> datetime.date:
    return month + relativedelta(months=1)


def earliest_reportable_day(day: datetime.date) -> datetime.date:
    """The earliest date a key's usage may be reported for on ``day``.

    The start of the year, the longest period a limit has, or of the previous
    month when that is earlier: an agent may still send the last days of a
    month just after it ended, and a report refused for them would lose the
    current days with it. Earlier dates could not count for any limit.
    """
    previous_month = (day.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    return min(spending_limits.period_bounds(YEAR, day)[0], previous_month)


def record_days(api_key: models.ResourceApiKey, daily_usages) -> None:
    """Store a key's usage per date; a date reported again replaces its amount.

    One statement for the whole report, which holds the key's lock meanwhile.
    """
    models.ResourceApiKeyDailyUsage.objects.bulk_create(
        [
            models.ResourceApiKeyDailyUsage(
                api_key=api_key,
                component_type=item["type"],
                date=item["date"],
                usage=item["amount"],
            )
            for item in daily_usages
        ],
        update_conflicts=True,
        unique_fields=["api_key", "component_type", "date"],
        update_fields=["usage", "modified"],
    )


def month_from_days(api_key: models.ResourceApiKey, month: datetime.date) -> dict:
    """The key's usage in ``month`` per component type that has daily amounts in it."""
    rows = (
        models.ResourceApiKeyDailyUsage.objects.filter(
            api_key=api_key, date__gte=month, date__lt=_month_end(month)
        )
        .values("component_type")
        .annotate(total=Sum("usage"))
    )
    return {
        row["component_type"]: spending_limits.json_number(row["total"]) for row in rows
    }


def types_with_days(api_key: models.ResourceApiKey, month: datetime.date) -> set:
    """The component types the key has daily amounts for in ``month``."""
    return set(
        models.ResourceApiKeyDailyUsage.objects.filter(
            api_key=api_key, date__gte=month, date__lt=_month_end(month)
        ).values_list("component_type", flat=True)
    )


@transaction.atomic
def rename_component_type(offering, old_type: str, new_type: str) -> None:
    """Move the limits, usage and daily rows of ``offering``'s keys to a renamed component type."""
    # Locked, as a usage report locks the key: one running meanwhile would
    # otherwise write the old type back, or lose its amount.
    keys = models.ResourceApiKey.objects.select_for_update().filter(
        resource__offering=offering
    )
    for api_key in keys:
        changed = []
        # The renamed component's limits are the ones set for it; any the key
        # still holds under the new type belong to a component gone since.
        if api_key.spending_limits and old_type in api_key.spending_limits:
            api_key.spending_limits[new_type] = api_key.spending_limits.pop(old_type)
            changed.append("spending_limits")
        # Usage the key holds under the new type was spent all the same: the
        # renamed component's is added to it, so a total never drops.
        if api_key.current_usages and old_type in api_key.current_usages:
            _move_usage(api_key.current_usages, old_type, new_type)
            changed.append("current_usages")
        history = api_key.usage_history or {}
        if any(old_type in usages for usages in history.values()):
            for usages in history.values():
                if old_type in usages:
                    _move_usage(usages, old_type, new_type)
            changed.append("usage_history")
        if changed:
            # update() leaves modified alone, which the agent's sweep reads as
            # the time the key entered its state.
            models.ResourceApiKey.objects.filter(pk=api_key.pk).update(
                **{field: getattr(api_key, field) for field in changed}
            )
    rows = models.ResourceApiKeyDailyUsage.objects.filter(
        api_key__resource__offering=offering
    )
    # A date the key has under both types would break the unique constraint:
    # its amounts are added up as well.
    clashing = rows.filter(component_type=old_type).filter(
        Exists(
            models.ResourceApiKeyDailyUsage.objects.filter(
                api_key=OuterRef("api_key"),
                date=OuterRef("date"),
                component_type=new_type,
            )
        )
    )
    for row in clashing:
        models.ResourceApiKeyDailyUsage.objects.filter(
            api_key_id=row.api_key_id, date=row.date, component_type=new_type
        ).update(usage=F("usage") + row.usage)
        row.delete()
    rows.filter(component_type=old_type).update(component_type=new_type)


def _move_usage(usages: dict, old_type: str, new_type: str) -> None:
    """Move ``old_type``'s amount onto ``new_type``, adding it to any held there."""
    moved = Decimal(str(usages.pop(old_type)))
    if new_type in usages:
        usages[new_type] = spending_limits.json_number(
            Decimal(str(usages[new_type])) + moved
        )
    else:
        usages[new_type] = spending_limits.json_number(moved)


# Rows older than this many whole years before the current one are pruned: no
# limit reads them, and a report can no longer be dated in them.
DAILY_USAGE_RETENTION_YEARS = 1


def _limit_periods(api_key: models.ResourceApiKey) -> set[str]:
    return {
        period
        for limits in (api_key.spending_limits or {}).values()
        if isinstance(limits, dict)
        for period, amount in limits.items()
        if amount is not None and period in PERIODS
    }


def rows_since(api_key: models.ResourceApiKey, day: datetime.date):
    """The earliest date of the daily rows the key's limits read on ``day``.

    None when they read none: a month limit reads the key's month figure, and
    a key without a limit reads nothing, and a total reads the key's usage
    history. Day and week read the rows of their period; quarter and year
    those of the months before the current one.
    """
    starts = [
        spending_limits.period_bounds(period, day)[0]
        for period in _limit_periods(api_key)
        if period not in WITHOUT_DAILY_USAGE
    ]
    return min(starts, default=None)


class _HasLimitFor(Func):
    """Whether a key's ``spending_limits`` has a limit for ``period`` on any component."""

    output_field = BooleanField()

    def __init__(self, expression, period):
        if period not in PERIODS:
            raise ValueError(f"Unknown spending limit period: {period}")
        # The period comes from a fixed list, so it may be written in the SQL.
        self.template = (
            f"jsonb_path_exists(%(expressions)s, '$.* ? (@.{period} != null)')"
        )
        super().__init__(expression)


def daily_rows_read(day: datetime.date, api_key_field="api_key") -> Q:
    """The daily rows that some key's limits read on ``day``, as ``rows_since`` has it.

    For a listing to load the rows of a page of keys in one query, and only
    those: most keys hold no limit that reads them.
    """
    limits = F(f"{api_key_field}__spending_limits")
    condition = Q(pk__in=[])
    for period in PERIODS:
        if period in WITHOUT_DAILY_USAGE:
            continue
        condition |= Q(
            Q(date__gte=spending_limits.period_bounds(period, day)[0]),
            _HasLimitFor(limits, period),
        )
    return condition


def _daily_rows(api_key: models.ResourceApiKey, day: datetime.date) -> list:
    # A listing prefetches the rows some key reads (daily_rows_read), so a
    # page of keys costs one query for all of them.
    since = rows_since(api_key, day)
    if since is None:
        return []
    prefetched = getattr(api_key, "_prefetched_objects_cache", {})
    if "daily_usages" in prefetched:
        return [row for row in prefetched["daily_usages"] if row.date >= since]
    return list(api_key.daily_usages.filter(date__gte=since))


def prune_daily_usage(day: datetime.date) -> int:
    """Delete the daily rows no limit reads any more, and return how many.

    Those of the current year and the one before are kept, deleted keys'
    included: the previous year is still shown while it is recent.
    """
    keep_from = datetime.date(day.year - DAILY_USAGE_RETENTION_YEARS, 1, 1)
    deleted, _ = models.ResourceApiKeyDailyUsage.objects.filter(
        date__lt=keep_from
    ).delete()
    return deleted


def _month_usage(api_key, component_type, month: datetime.date) -> Decimal:
    if api_key.usage_period != month:
        return Decimal(0)
    return Decimal(str((api_key.current_usages or {}).get(component_type, 0)))


def total_usage(api_key, component_type) -> Decimal:
    """Usage of ``component_type`` in every month the key reported in.

    The months before ``usage_period`` come from the history and the month it
    holds from ``current_usages``, which is not in the history yet.
    """
    closed = sum(
        (
            Decimal(str(usages.get(component_type, 0)))
            for month, usages in (api_key.usage_history or {}).items()
            if not api_key.usage_period or month != f"{api_key.usage_period:%Y-%m}"
        ),
        Decimal(0),
    )
    return closed + Decimal(str((api_key.current_usages or {}).get(component_type, 0)))


def raised(kept: dict | None, usages: dict) -> dict:
    """``kept`` with each component of ``usages`` raised to it, never lowered.

    A month figure only grows: a lower report leaves the amount ``kept``.
    """
    kept = kept or {}
    result = dict(kept)
    for component_type, amount in usages.items():
        if component_type not in kept or Decimal(str(amount)) > Decimal(
            str(kept[component_type])
        ):
            result[component_type] = amount
    return result


def close_month(api_key: models.ResourceApiKey) -> None:
    """Keep the month the key holds in its history, as its usage moves on to a later one.

    Called before ``current_usages`` is replaced by a later month's. A month
    already in the history keeps the higher figure of each component, so a
    report sent again cannot count it twice.
    """
    if not api_key.usage_period or not api_key.current_usages:
        return
    raise_closed_month(api_key, api_key.usage_period, api_key.current_usages)


def raise_closed_month(
    api_key: models.ResourceApiKey, month: datetime.date, usages: dict
) -> None:
    """Raise ``month``'s figures in the key's history to ``usages``, never lowering them.

    For the month the key leaves (``close_month``), and for a month figure an
    agent sends after the key moved on, such as the usage of a month's last
    minutes reported just after midnight: without it, a total would miss the
    tail of every month.
    """
    key = f"{month:%Y-%m}"
    history = dict(api_key.usage_history or {})
    history[key] = raised(history.get(key), usages)
    api_key.usage_history = history


def count_late_days(api_key: models.ResourceApiKey, months) -> None:
    """Raise each closed month's figure in the history to the sum of its days.

    For days reported after the key moved on to a later month: one query for
    all of ``months``. As for the current month, a figure never drops, so a
    month total reported before the days stays as a floor.
    """
    if not months:
        return
    rows = (
        models.ResourceApiKeyDailyUsage.objects.filter(
            api_key=api_key,
            date__gte=min(months),
            date__lt=_month_end(max(months)),
        )
        .annotate(month=TruncMonth("date"))
        .values("month", "component_type")
        .annotate(total=Sum("usage"))
    )
    history = dict(api_key.usage_history or {})
    for row in rows:
        if row["month"] not in months:
            continue
        key = f"{row['month']:%Y-%m}"
        kept = dict(history.get(key, {}))
        kept[row["component_type"]] = spending_limits.json_number(
            max(Decimal(str(kept.get(row["component_type"], 0))), row["total"])
        )
        history[key] = kept
    api_key.usage_history = history


def period_usage(api_key, component_type, period, day, rows) -> Decimal:
    """Usage of ``component_type`` in the ``period`` that contains ``day``."""
    if period == TOTAL:
        return total_usage(api_key, component_type)
    start, end = spending_limits.period_bounds(period, day)
    month = day.replace(day=1)

    def days(until):
        return sum(
            (
                row.usage
                for row in rows
                if row.component_type == component_type and start <= row.date < until
            ),
            Decimal(0),
        )

    if period == MONTH:
        return _month_usage(api_key, component_type, month)
    if period in SpendingLimitPeriods.DAILY:
        return days(end)
    return days(month) + _month_usage(api_key, component_type, month)


def get_status(api_key: models.ResourceApiKey, day=None) -> list[dict]:
    """Each limit of the key with its usage so far, its reset time and whether it is reached."""
    if not api_key.spending_limits:
        return []
    day = day or spending_limits.utc_today()
    rows = _daily_rows(api_key, day)
    result = []
    for component_type, limits in api_key.spending_limits.items():
        for period in PERIODS:
            if limits.get(period) is None:
                continue
            limit = Decimal(str(limits[period]))
            used = period_usage(api_key, component_type, period, day, rows)
            result.append(
                {
                    "component_type": component_type,
                    "period": period,
                    "limit": limit,
                    "used": used,
                    "resets_at": spending_limits.resets_at(period, day),
                    "reached": used >= limit,
                }
            )
    return result


def reached_limits(api_key: models.ResourceApiKey) -> list[dict]:
    return [status for status in get_status(api_key) if status["reached"]]
