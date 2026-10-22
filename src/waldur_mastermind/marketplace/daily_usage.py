"""Usage reported per day.

A provider may report a usage-based component's usage per UTC date instead of
as the month's running total. Waldur needs that to measure day and week spending
limits (see ``spending_limits``): a running total overwritten on every report
loses where one day ends and the next begins.

Each reported date replaces that date's amount in ``ComponentDailyUsage``. The
month's ``ComponentUsage`` rows, which invoicing and every monthly figure read,
are then recomputed as the sum of the month's days, per plan: a day belongs to
the plan period in force at its end, or now for today. A monthly figure and the
days it is made of therefore cannot disagree, and the invoice follows the same
path as for a monthly report.

Once a month has daily amounts, a monthly total for it is rejected: it would be
overwritten by the next daily report. A month row written before the first daily
report is replaced by the days, so an integration that starts reporting per day
mid-month must report every day of the month so far.
"""

import datetime

from dateutil.relativedelta import relativedelta
from django.db import transaction

from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.enums import MissingUsagePolicies


def has_daily_usage(resource, component, billing_period: datetime.date) -> bool:
    """Whether the month starting on ``billing_period`` has daily amounts."""
    return models.ComponentDailyUsage.objects.filter(
        resource=resource,
        component=component,
        date__gte=billing_period,
        date__lt=billing_period + relativedelta(months=1),
    ).exists()


def _end_of_day(day: datetime.date, now: datetime.datetime) -> datetime.datetime:
    """The moment whose plan period ``day`` belongs to: its last instant, or now."""
    end = datetime.datetime.combine(
        day + datetime.timedelta(days=1), datetime.time.min, datetime.UTC
    ) - datetime.timedelta(microseconds=1)
    return min(end, now)


def record(resource, component, amounts: dict, user, now: datetime.datetime):
    """Store ``amounts`` ({date: amount}) and recompute the months they fall in.

    The month total is read back from the days, so two reports for one
    resource must not interleave: each would sum without the other's day and
    the last to commit would drop it.
    """
    with transaction.atomic():
        # Written through the locked row, so the report handlers read the
        # spending limits as committed rather than the caller's copy.
        resource = models.Resource.objects.select_for_update().get(pk=resource.pk)
        for day, amount in amounts.items():
            models.ComponentDailyUsage.objects.update_or_create(
                resource=resource,
                component=component,
                date=day,
                defaults={"usage": amount, "modified_by": user},
            )
        for billing_period in sorted({day.replace(day=1) for day in amounts}):
            derive_month(resource, component, billing_period, user, now)


def derive_month(resource, component, billing_period, user, now):
    """Set the month's ``ComponentUsage`` rows to the sum of its days, per plan."""
    days = models.ComponentDailyUsage.objects.filter(
        resource=resource,
        component=component,
        date__gte=billing_period,
        date__lt=billing_period + relativedelta(months=1),
    )
    # Consecutive periods of one plan share a row (see _get_current_usage_row),
    # so days are summed per plan and the row points at the latest period.
    totals = {}
    for day in days:
        moment = _end_of_day(day.date, now)
        plan_period = utils.get_plan_period_for_billing(resource, moment)
        key = plan_period.plan_id if plan_period else None
        total, latest_moment, latest_period = totals.get(key, (0, None, None))
        if latest_moment is None or moment > latest_moment:
            latest_moment, latest_period = moment, plan_period
        totals[key] = (total + day.usage, latest_moment, latest_period)

    written = set()
    for total, moment, plan_period in totals.values():
        row = utils._get_current_usage_row(
            resource, component, billing_period, plan_period
        )
        if row is None:
            row = models.ComponentUsage(
                resource=resource,
                component=component,
                billing_period=billing_period,
            )
        row.plan_period = plan_period
        row.usage = total
        row.date = moment
        row.missing_usage_policy = MissingUsagePolicies.NONE
        row.modified_by = user
        row.save()
        written.add(row.pk)

    # A month total reported before the first daily report, under a plan no
    # day falls in, would otherwise be invoiced on top of the days.
    stale = models.ComponentUsage.objects.filter(
        resource=resource, component=component, billing_period=billing_period
    ).exclude(pk__in=written)
    for row in stale:
        if row.usage:
            row.usage = 0
            row.modified_by = user
            row.save()
