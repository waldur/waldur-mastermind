"""Spending limits on usage-based components.

A usage-based component is billed on what was used. Its provider may also let
the customer cap that use: ``OfferingComponent.spending_limit_periods`` names
the calendar periods offered, each with an optional default (what the order form
starts from) and maximum, and ``Resource.spending_limits`` holds the customer's
amounts as ``{component_type: {period: amount}}``.

Limits are never billed, and never stored in ``Resource.limits``: that field
holds the quantities of limit-based components, which are invoiced, validated on
plan switches and renewals, and pushed to backends as quotas.

Every period compares a different span of the same usage. Day and week sum the
per-day rows (``ComponentDailyUsage``, see ``daily_usage``); month, quarter and
year sum the monthly ``ComponentUsage`` rows that invoicing reads. Periods are
calendar periods in UTC, and a week starts on Monday. ``total`` is the one
period that is not a calendar period: it sums every monthly row since the
resource started and never resets, so a restriction it holds lifts only when
the limit is raised or cleared.

A reached limit applies the offering's ``action_on_usage_limit`` through
``utils.evaluate_usage_limit_restriction``, which lifts the restriction once
every limit is under again. ``Resource.usage_limit_resumes_at`` records when the
reached limits reset, and ``tasks.lift_reset_spending_limit_restrictions``
re-evaluates the resource once that time has passed, so a daily pause ends at
midnight without a new usage report. It stays empty while a reached total
holds the resource.
"""

import datetime
from decimal import Decimal, InvalidOperation

from dateutil.relativedelta import relativedelta
from django.db.models import Max, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_mastermind.marketplace import billing_mode, models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    ResourceStates,
    SpendingLimitPeriods,
)

PERIODS = SpendingLimitPeriods.ORDER
BOUND_KEYS = ("default", "maximum")
# The precision of ComponentUsage.usage, which limits are compared with.
DECIMAL_PLACES = 2
MAX_AMOUNT = Decimal(10) ** 18
# A resource being provisioned may get its limits before it is ready; one being
# terminated has no use for them.
EDITABLE_STATES = (
    ResourceStates.CREATING,
    ResourceStates.OK,
    ResourceStates.ERRED,
    ResourceStates.UPDATING,
)


def utc_today() -> datetime.date:
    return timezone.now().astimezone(datetime.UTC).date()


def period_bounds(period: str, day: datetime.date):
    """The first day of the ``period`` that contains ``day``, and the first day after it.

    ``total`` has no bounds: callers handle it before asking.
    """
    if period == SpendingLimitPeriods.DAY:
        start = day
        end = day + datetime.timedelta(days=1)
    elif period == SpendingLimitPeriods.WEEK:
        start = day - datetime.timedelta(days=day.weekday())
        end = start + datetime.timedelta(days=7)
    elif period == SpendingLimitPeriods.MONTH:
        start = day.replace(day=1)
        end = start + relativedelta(months=1)
    elif period == SpendingLimitPeriods.QUARTER:
        start = day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)
        end = start + relativedelta(months=3)
    elif period == SpendingLimitPeriods.YEAR:
        start = day.replace(month=1, day=1)
        end = start + relativedelta(years=1)
    else:
        raise ValueError(f"Unknown spending limit period: {period}")
    return start, end


def resets_at(period: str, day: datetime.date) -> datetime.datetime | None:
    """When the ``period`` that contains ``day`` ends and its usage starts from zero.

    None for ``total``, which never resets.
    """
    if period == SpendingLimitPeriods.TOTAL:
        return None
    _start, end = period_bounds(period, day)
    return datetime.datetime.combine(end, datetime.time.min, datetime.UTC)


def parse_amount(value, label) -> Decimal:
    """``value`` as a non-negative amount with at most two decimal places."""
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise serializers.ValidationError(_("%s must be a number.") % label)
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise serializers.ValidationError(_("%s must be a number.") % label)
    if not amount.is_finite():
        raise serializers.ValidationError(_("%s must be a number.") % label)
    if amount < 0:
        raise serializers.ValidationError(_("%s must not be negative.") % label)
    if amount >= MAX_AMOUNT:
        raise serializers.ValidationError(_("%s is too large.") % label)
    if amount != amount.quantize(Decimal(1).scaleb(-DECIMAL_PLACES)):
        raise serializers.ValidationError(
            _("%(label)s accepts at most %(places)s decimal places.")
            % {"label": label, "places": DECIMAL_PLACES}
        )
    return amount


def json_number(amount: Decimal) -> int | float:
    if amount == amount.to_integral_value():
        return int(amount)
    return float(amount)


def validate_order(amounts: dict, label: str):
    """A shorter period's amount may not exceed a longer one's."""
    present = [period for period in PERIODS if period in amounts]
    for shorter, longer in zip(present, present[1:]):
        if amounts[shorter] > amounts[longer]:
            raise serializers.ValidationError(
                _(
                    "%(label)s: the %(shorter)s amount (%(shorter_amount)s) is above "
                    "the %(longer)s amount (%(longer_amount)s)."
                )
                % {
                    "label": label,
                    "shorter": shorter,
                    "shorter_amount": amounts[shorter],
                    "longer": longer,
                    "longer_amount": amounts[longer],
                }
            )


def validate_component_periods(value, billing_type: str) -> dict:
    """Clean ``OfferingComponent.spending_limit_periods`` for a component billed as ``billing_type``."""
    if not value:
        return {}
    if billing_type != BillingTypes.USAGE:
        raise serializers.ValidationError(
            _("Spending limits are only available on usage-based components.")
        )
    if not isinstance(value, dict):
        raise serializers.ValidationError(
            _("Spending limit periods must be an object keyed by period.")
        )
    unknown = sorted(set(value) - set(PERIODS))
    if unknown:
        raise serializers.ValidationError(
            _("Unknown spending limit periods: %(unknown)s. Choose from %(periods)s.")
            % {"unknown": ", ".join(unknown), "periods": ", ".join(PERIODS)}
        )

    cleaned = {}
    for period in PERIODS:
        if period not in value:
            continue
        bounds = value[period] or {}
        if not isinstance(bounds, dict):
            raise serializers.ValidationError(
                _("The %s period must be an object with a default and a maximum.")
                % period
            )
        unknown_keys = sorted(set(bounds) - set(BOUND_KEYS))
        if unknown_keys:
            raise serializers.ValidationError(
                _("The %(period)s period does not accept %(keys)s.")
                % {"period": period, "keys": ", ".join(unknown_keys)}
            )
        entry = {}
        for key in BOUND_KEYS:
            if bounds.get(key) is not None:
                entry[key] = parse_amount(
                    bounds[key],
                    _("The %(period)s %(key)s") % {"period": period, "key": key},
                )
        if "default" in entry and "maximum" in entry:
            if entry["default"] > entry["maximum"]:
                raise serializers.ValidationError(
                    _("The %s default is above its maximum.") % period
                )
        cleaned[period] = entry

    validate_order(
        {
            period: entry["default"]
            for period, entry in cleaned.items()
            if "default" in entry
        },
        _("Defaults"),
    )
    return {
        period: {key: json_number(amount) for key, amount in entry.items()}
        for period, entry in cleaned.items()
    }


def capped_components(offering, plan) -> dict:
    """The components of ``offering`` a customer may set spending limits on under ``plan``.

    A plan may bill a plugin's usage component by limit; it then holds a
    quantity, not a spend, and takes no spending limit.
    """
    resolved = billing_mode.ResolvedPlan(offering, plan)
    return {
        effective.type: effective.component
        for effective in resolved.components.values()
        if effective.billing_type == BillingTypes.USAGE
        and effective.component.spending_limit_periods
    }


def validate_resource_limits(value, offering, plan) -> dict:
    """Clean the spending limits set on a resource of ``offering`` under ``plan``.

    A null amount clears that period's limit, and a component left without any
    limit is dropped.
    """
    if not value:
        return {}
    if not isinstance(value, dict):
        raise serializers.ValidationError(
            _("Spending limits must be an object keyed by component type.")
        )
    components = capped_components(offering, plan)
    cleaned = {}
    for component_type, periods in value.items():
        component = components.get(component_type)
        if periods is None or (
            isinstance(periods, dict) and all(v is None for v in periods.values())
        ):
            # Clearing is allowed even after the provider stops offering limits.
            continue
        if component is None:
            raise serializers.ValidationError(
                {component_type: _("This component does not accept spending limits.")}
            )
        if not periods:
            continue
        if not isinstance(periods, dict):
            raise serializers.ValidationError(
                {
                    component_type: _(
                        "Spending limits must be an object keyed by period."
                    )
                }
            )
        offered = component.spending_limit_periods
        amounts = {}
        for period, raw in periods.items():
            if raw is None:
                # Clearing is allowed even after the provider stops offering it.
                continue
            if period not in offered:
                raise serializers.ValidationError(
                    {
                        component_type: _(
                            "A %(period)s spending limit is not offered. "
                            "Offered: %(offered)s."
                        )
                        % {
                            "period": period,
                            "offered": ", ".join(p for p in PERIODS if p in offered),
                        }
                    }
                )
            amount = parse_amount(raw, _("The %s limit") % period)
            maximum = (offered[period] or {}).get("maximum")
            if maximum is not None and amount > Decimal(str(maximum)):
                raise serializers.ValidationError(
                    {
                        component_type: _(
                            "The %(period)s limit may not exceed %(maximum)s."
                        )
                        % {"period": period, "maximum": maximum}
                    }
                )
            amounts[period] = amount
        try:
            validate_order(amounts, _("Limits"))
        except serializers.ValidationError as error:
            raise serializers.ValidationError({component_type: error.detail})
        if amounts:
            cleaned[component_type] = {
                period: json_number(amounts[period])
                for period in PERIODS
                if period in amounts
            }
    return cleaned


def period_usage(resource, component, period, day: datetime.date) -> Decimal:
    """Usage of ``component`` in the ``period`` that contains ``day``.

    For ``total``, every month the resource has usage for.
    """
    rows = models.ComponentUsage.objects.filter(resource=resource, component=component)
    if period != SpendingLimitPeriods.TOTAL:
        start, end = period_bounds(period, day)
        if period in SpendingLimitPeriods.DAILY:
            total = models.ComponentDailyUsage.objects.filter(
                resource=resource, component=component, date__gte=start, date__lt=end
            ).aggregate(total=Sum("usage"))["total"]
            return total or Decimal(0)
        rows = rows.filter(billing_period__gte=start, billing_period__lt=end)
    rows = rows.select_related("plan_period__plan")
    # A month billed under a plan that counts the component as a limit holds a
    # peak quantity, not usage.
    return sum(
        (
            row.usage
            for row in rows
            if billing_mode.resolve_component(
                component, row.plan_period.plan if row.plan_period else resource.plan
            ).billing_type
            == BillingTypes.USAGE
        ),
        Decimal(0),
    )


def last_reported_at(resource, component) -> datetime.datetime | None:
    """When usage of ``component`` was last reported for ``resource``."""
    return models.ComponentUsage.objects.filter(
        resource=resource, component=component
    ).aggregate(latest=Max("modified"))["latest"]


def get_status(resource, day: datetime.date | None = None) -> list[dict]:
    """Each spending limit of ``resource`` with its usage so far and its reset time."""
    if not resource.spending_limits:
        return []
    day = day or utc_today()
    resolved = billing_mode.resolve_for_resource(resource)
    result = []
    for component_type, limits in resource.spending_limits.items():
        # A limit stays in force when the provider stops offering its period,
        # so the customer's cap is not dropped under them. It is skipped while
        # the plan bills the component by limit.
        effective = resolved.get(component_type)
        if effective is None or effective.billing_type != BillingTypes.USAGE:
            continue
        component = effective.component
        reported_at = last_reported_at(resource, component)
        for period in PERIODS:
            if limits.get(period) is None:
                continue
            limit = Decimal(str(limits[period]))
            used = period_usage(resource, component, period, day)
            result.append(
                {
                    "component_type": component_type,
                    "period": period,
                    "limit": limit,
                    "used": used,
                    "resets_at": resets_at(period, day),
                    "reached": used >= limit,
                    "last_reported_at": reported_at,
                }
            )
    return result


def reached_limits(resource) -> list[dict]:
    return [status for status in get_status(resource) if status["reached"]]


def resumes_at(reached: list[dict]) -> datetime.datetime | None:
    """When every reached limit has reset, so a restriction they hold lifts.

    None also while a reached total holds it: that never resets, and lifts
    only when the limit is raised or cleared.
    """
    if not reached or any(status["resets_at"] is None for status in reached):
        return None
    return max(status["resets_at"] for status in reached)


def describe(reached: list[dict]) -> str:
    """The reached limits in words, for an event message."""
    return ", ".join(
        f"{status['period']} limit of {status['component_type']}" for status in reached
    )
