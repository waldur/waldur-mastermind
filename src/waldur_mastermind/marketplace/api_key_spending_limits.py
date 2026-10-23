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
component offers them to resources. A component the resource's plan does not
charge for takes no limit.
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
there and no earlier one.

Enforcement is the key's own: ``resource_api_keys.record_usage`` pauses the key
alone once a limit is reached, records in ``limit_resumes_at`` when every reached
limit resets, and ``resource_api_keys.resume_keys_under_limit`` resumes it then.
"""

import datetime
from decimal import ROUND_UP, Decimal, InvalidOperation

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_mastermind.marketplace import models, spending_limits
from waldur_mastermind.marketplace.enums import SpendingLimitPeriods

PERIODS = SpendingLimitPeriods.ORDER
MONTH = SpendingLimitPeriods.MONTH


LIMITS_OPTION = "api_key_limits"
DAILY_USAGE_OPTION = "api_key_daily_usage"


def limits_offered(offering) -> bool:
    """Whether consumers may set limits on the offering's keys; on unless turned off."""
    return (offering.plugin_options or {}).get(LIMITS_OPTION) is not False


def daily_usage_reported(offering) -> bool:
    """Whether the provider declares that each key's usage is reported per day."""
    return (offering.plugin_options or {}).get(DAILY_USAGE_OPTION) is True


def free_component_types(resource) -> set[str]:
    """The component types the resource's plan prices at zero.

    Nothing is spent on such a component, so a limit on it would only take
    room in the key's settings. A component the plan does not price at all is
    not counted: the plan may simply predate it.
    """
    if resource.plan_id is None:
        return set()
    return set(
        models.PlanComponent.objects.filter(
            plan_id=resource.plan_id, price=0
        ).values_list("component__type", flat=True)
    )


def offered_periods(resource) -> dict[str, set[str]]:
    """The periods a key of ``resource`` may limit, per component type.

    Every component of the offering is listed, with no period at all where
    limits are off for the offering or the plan does not charge for it.
    """
    offering = resource.offering
    types = list(offering.components.values_list("type", flat=True))
    if not limits_offered(offering):
        return {component_type: set() for component_type in types}
    free = free_component_types(resource)
    capped = (
        spending_limits.capped_components(offering, resource.plan)
        if daily_usage_reported(offering)
        else {}
    )
    return {
        component_type: set()
        if component_type in free
        else {MONTH}
        | set(
            capped[component_type].spending_limit_periods
            if component_type in capped
            else ()
        )
        for component_type in types
    }


def _not_offered(resource, component_type, period, offered) -> str:
    """Why a ``period`` limit is refused on ``component_type``."""
    if not limits_offered(resource.offering):
        return _("The offering does not offer limits on API keys.")
    if not offered:
        return _("%s is not charged for, so it takes no limit.") % component_type
    message = _("A %(period)s limit is not offered. Offered: %(offered)s.") % {
        "period": period,
        "offered": ", ".join(p for p in PERIODS if p in offered),
    }
    if period != MONTH and not daily_usage_reported(resource.offering):
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
                cap = resource_cap(
                    resource_limits.get(component_type) or {}, period
                )
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


# --- usage --------------------------------------------------------------------


def _month_end(month: datetime.date) -> datetime.date:
    return month + relativedelta(months=1)


def record_days(api_key: models.ResourceApiKey, daily_usages) -> None:
    """Store a key's usage per date; a date reported again replaces its amount."""
    for item in daily_usages:
        models.ResourceApiKeyDailyUsage.objects.update_or_create(
            api_key=api_key,
            component_type=item["type"],
            date=item["date"],
            defaults={"usage": item["amount"]},
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
        for field in ("spending_limits", "current_usages"):
            value = getattr(api_key, field)
            if value and old_type in value:
                value[new_type] = value.pop(old_type)
                changed.append(field)
        if changed:
            # update() leaves modified alone, which the agent's sweep reads as
            # the time the key entered its state.
            models.ResourceApiKey.objects.filter(pk=api_key.pk).update(
                **{field: getattr(api_key, field) for field in changed}
            )
    models.ResourceApiKeyDailyUsage.objects.filter(
        api_key__resource__offering=offering, component_type=old_type
    ).update(component_type=new_type)


def window_start(day: datetime.date) -> datetime.date:
    """The earliest date any period containing ``day`` reaches back to."""
    return min(spending_limits.period_bounds(period, day)[0] for period in PERIODS)


def _daily_rows(api_key: models.ResourceApiKey, since: datetime.date) -> list:
    # The listing prefetches the rows since window_start, so a page of keys
    # costs one query for all of them.
    prefetched = getattr(api_key, "_prefetched_objects_cache", {})
    if "daily_usages" in prefetched:
        return [row for row in prefetched["daily_usages"] if row.date >= since]
    return list(api_key.daily_usages.filter(date__gte=since))


def _month_usage(api_key, component_type, month: datetime.date) -> Decimal:
    if api_key.usage_period != month:
        return Decimal(0)
    return Decimal(str((api_key.current_usages or {}).get(component_type, 0)))


def period_usage(api_key, component_type, period, day, rows) -> Decimal:
    """Usage of ``component_type`` in the ``period`` that contains ``day``."""
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
    rows = _daily_rows(api_key, window_start(day))
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
