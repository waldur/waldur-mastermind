"""When a cost policy will cross its limit.

Clients used to derive this themselves and could not: `current_cost` is the
period's invoice total *less the credit still to be drawn*, and only the server
can simulate the latter. waldur/waldur-homeport#244 divided the remaining
headroom by the credit *burn rate* — the compensating side of that same
subtraction — and so told credit-funded projects their resources would be paused
within days, on policies that were not triggered at all. The same argument
`EstimatedCostPolicySerializer.get_current_cost` makes for the cost figure
applies to its projection, so the projection lives here.

The model is two-phase, because that is how the quantity actually behaves.
`current_cost` only grows on cost the credit does not absorb:

* **While the credit lasts** it grows slowly, by whatever the credit is not
  eligible to cover — a project buying outside the offerings named on the
  organization balance carries such cost, which is why a fully funded project
  sits at a small non-zero `current_cost` rather than exactly zero.
* **Once the credit is gone** compensation stops and the whole gross cost lands
  on the policy, so it grows at the full rate.

A projection that ignored the first phase would fire far too early; one that
ignored the second would never fire at all.
"""

import calendar
import datetime
import decimal

from waldur_mastermind.invoices import models as invoices_models

#: Days a month is assumed to have when turning a monthly credit draw into a
#: rate. The draw is a monthly figure, so a nominal month keeps the projection
#: stable between months rather than making the same balance run out on
#: different dates in February.
DAYS_PER_MONTH = decimal.Decimal(30)

#: How far ahead a projection is worth making. The rate comes from the current
#: month's spend, which says nothing credible about a date years out — limits,
#: credits and usage will all have moved by then — and a large limit against a
#: trickle of spend produces figures in the millions of days, which overflow
#: `datetime.date` outright. Beyond this the honest answer is that the limit is
#: not in sight, which is what null means everywhere else here. It also matches
#: what clients already do with the number: the credit burn-down chart plots a
#: policy marker only when the estimate is under a year.
MAX_PROJECTION_DAYS = 365


def _decimal(value) -> decimal.Decimal:
    if value is None:
        return decimal.Decimal(0)
    if isinstance(value, decimal.Decimal):
        return value
    return decimal.Decimal(str(value))


def _daily_draw(credit, gross_per_day) -> decimal.Decimal | None:
    """How fast the credit balance falls, per day.

    The contractual draw is the larger of last month's consumption and the
    guaranteed minimum, because the minimum is taken whether or not it is used.
    A credit with neither — a fresh allocation with ``apply_as_minimal_consumption``
    off, in its first month — is still spent on what it compensates, so the
    observed cost rate is the floor below which the draw cannot honestly be
    assumed to fall. ``None`` when it is not being drawn at all.
    """
    contractual = max(
        _decimal(getattr(credit, "consumption_last_month", 0)),
        _decimal(getattr(credit, "minimal_consumption", 0)),
    )
    daily = max(contractual / DAYS_PER_MONTH, max(decimal.Decimal(0), gross_per_day))
    return daily if daily > 0 else None


def _balance(credit) -> decimal.Decimal:
    spendable = getattr(credit, "spendable_value", None)
    return max(
        decimal.Decimal(0),
        _decimal(credit.value if spendable is None else spendable),
    )


def credit_days_remaining(credit, gross_per_day) -> decimal.Decimal | None:
    """How long the credit keeps compensating, in days.

    ``0`` when no credit applies, ``None`` when it never depletes. This is the
    point at which the projection switches from the slow rate to the full one.
    """
    if credit is None:
        return decimal.Decimal(0)
    daily = _daily_draw(credit, gross_per_day)
    if daily is None:
        return None
    return _balance(credit) / daily


def credit_days_to_limit(credit, limit_cost, gross_per_day) -> decimal.Decimal | None:
    """When the credit balance itself falls to ``limit_cost``.

    A cost policy does not fire on cost alone. Once the cost test passes,
    ``is_triggered`` returns ``credit.value <= limit_cost`` — so a project whose
    balance is still above the limit cannot trigger however much it spends, and
    a projection made from the cost crossing alone dates an event the balance
    forbids. This is the gate the cost projection has to clear.

    ``0`` when no credit applies or the balance is already at or below the
    limit; ``None`` when the balance never reaches it.
    """
    if credit is None:
        return decimal.Decimal(0)
    excess = _balance(credit) - _decimal(limit_cost)
    if excess <= 0:
        return decimal.Decimal(0)
    daily = _daily_draw(credit, gross_per_day)
    if daily is None:
        return None
    return excess / daily


def project_eta_days(
    *,
    limit_cost,
    current_cost,
    gross_this_month,
    uncompensated_this_month,
    credit_days,
    credit_limit_days,
    period,
    today: datetime.date,
) -> int | None:
    """Days until ``current_cost`` crosses ``limit_cost``.

    ``0`` means the limit is already exceeded — measured, not projected, and
    never the result of a projection, so a client can render it as a breach
    rather than as "imminent". ``None`` means no honest projection exists, which
    is the common case and must not be rendered as a date.

    ``credit_days`` comes from :func:`credit_days_remaining` and says when the
    slow phase ends. ``credit_limit_days`` comes from
    :func:`credit_days_to_limit` and is the gate: the policy cannot fire until
    the balance is down to the limit, whatever the cost is doing.
    """
    limit = _decimal(limit_cost)
    current = _decimal(current_cost)

    # Strictly greater, to agree with `_is_triggered`. A cost that has exactly
    # reached the limit has not crossed it.
    cost_crossed = current > limit
    if cost_crossed and credit_limit_days == 0:
        return 0

    elapsed = decimal.Decimal(today.day)
    gross = max(decimal.Decimal(0), _decimal(gross_this_month))

    # The rate once compensation stops: the whole cost lands on the policy.
    fast = gross / elapsed
    # The rate while the credit lasts, measured from the policy's own metric so
    # that it shares a basis with the level it is compared against. With no
    # credit compensating, nothing is deducted and this equals `fast`.
    slow = max(decimal.Decimal(0), _decimal(uncompensated_this_month)) / elapsed

    remaining = limit - current

    if cost_crossed:
        days = decimal.Decimal(0)
    elif slow > 0 and (credit_days is None or remaining <= slow * credit_days):
        days = remaining / slow
    elif credit_days is None:
        # The credit never runs out and nothing is accruing past it, so the
        # limit is never reached.
        return None
    elif fast > 0:
        # Whatever the slow phase does not consume is consumed at the full rate
        # once the credit is gone.
        days = credit_days + (remaining - slow * credit_days) / fast
    else:
        # Nothing is being spent at all.
        return None

    if credit_limit_days is None:
        # The balance never falls to the limit, so the gate never opens.
        return None
    # Both conditions have to hold, so the later of the two is when it fires.
    days = max(days, _decimal(credit_limit_days))

    # Rounded up, never down: a projection of less than a day is still a day
    # away, and truncating it to 0 would claim the limit had already been
    # crossed. 0 is reserved for that, and is only ever returned above.
    days = int(days.to_integral_value(rounding=decimal.ROUND_CEILING))

    if days > MAX_PROJECTION_DAYS:
        return None

    if period == invoices_models.PeriodMixin.Periods.MONTH_1:
        # A one-month policy starts its total again at month end, so a date past
        # it would be measured against a total that does not exist yet. The
        # longer windows only drop their oldest month — a twelve-month total
        # barely moves — so they are left to the horizon above rather than being
        # cut off at the end of the month, which hid real dates.
        days_left = calendar.monthrange(today.year, today.month)[1] - today.day
        if days > days_left:
            return None

    return days
