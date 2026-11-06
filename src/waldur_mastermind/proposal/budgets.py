"""A call's resource budget: what a proposal may request of each component
of a call offering, and how much of it the call awards.

What a proposal may request is set per component of a call offering
(``CallOfferingComponent``): the least and the most, or a fixed amount, with
the amount the request form starts from. These are in the component's
measured unit and per its limit period -- the value a request's limits carry
-- and bound the proposal's request summed over its items, whatever its
round.

How much the call awards is set by resource pools (``CallResourcePool``) on
those components: one over the whole call, one per round, or both. A
proposal draws on the call-wide pool of each component it is awarded and on
the pool of its own round, and its award fits only when it fits every pool
it draws on. "1M per call, at most 400k per cut-off" is a call-wide pool of
1M and a pool of 400k on each round.

How an award counts against a pool. A pool's total is in the component's
measured unit over the whole length of each award, so awards of different
lengths, and components limited per different periods, add up:

* a component limited per ``total`` counts its amount once;
* a component limited per month, quarter or year counts its amount once per
  such period the item runs, a period begun counting whole. The item runs
  for its own subscription (``prepaid_duration_months``), else for the
  project length chosen from the call's list (the awarded one for an award,
  the requested one for a request), else for the call's fixed duration in
  whole months (its days over 30.4375, rounded, at least one). With no
  length known, the amount counts once.

``unit_factor`` relates the measured unit to the backend's units at the
provider and plays no part here: rules, pools and limits are all in the
measured unit.

What a pool has awarded is counted from the awarded resources -- what the
allocation decision grants, which may differ from the request -- of the
proposals it covers that hold an award: accepted ones, and those whose
allocation decision approved them and that are still in review (awaiting the
applicant's response, or held until the round's results are published). An
award therefore reserves its amounts the moment it is approved, and releases
them when the applicant declines it, the proposal is rejected or cancelled,
or a held decision is reopened. An item moved to another offering counts
against that offering's pools.

Approving an award past what remains is refused unless overridden with a
reason: by the call manager in ``soft`` mode, by staff alone in ``hard``. The
check runs under row locks taken in one order everywhere -- the proposal,
its round, then the pools by primary key -- so two decisions cannot together
exceed a total, and cannot deadlock.
"""

import math
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db.models import Exists, OuterRef, Q
from django.utils.translation import gettext_lazy as _

from waldur_core.core.utils import get_system_robot
from waldur_mastermind.marketplace.enums import LimitPeriods
from waldur_mastermind.marketplace.models import PlanComponent
from waldur_mastermind.proposal import activity, models
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    BudgetEnforcement,
    ProposalStates,
    RequestedOfferingStates,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)

# Proposals that asked for something: submitted, whatever came of it since,
# but withdrawn ones.
REQUESTING_STATES = (
    ProposalStates.SUBMITTED,
    ProposalStates.IN_REVIEW,
    ProposalStates.ACCEPTED,
    ProposalStates.REJECTED,
)

# Months in each limit period a periodic amount is counted per; a ``total``
# limit is counted once.
PERIOD_MONTHS = {
    LimitPeriods.MONTH: 1,
    LimitPeriods.QUARTERLY: 3,
    LimitPeriods.ANNUAL: 12,
}
DAYS_PER_MONTH = Decimal("30.4375")


class BudgetExceeded(ValueError):
    """Approving the award would take a pool past its offered total."""


def to_decimal(value):
    """A limit value as a Decimal; None for anything that is not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def json_number(value):
    """A Decimal as the JSON number limits are stored as."""
    value = Decimal(value)
    return int(value) if value == value.to_integral() else float(value)


def _format(value) -> str:
    value = Decimal(value)
    return f"{value.normalize():f}" if value == value.to_integral() else f"{value:f}"


# -- counting an amount against a pool -------------------------------------------


def item_months(attributes, chosen_months=None, fixed_days=None):
    """How many months an item runs, as far as is known; None when nothing
    says. Its own subscription first, then the project length chosen from
    the call's list, then the call's fixed duration in whole months."""
    stored = (attributes or {}).get("prepaid_duration_months")
    if stored is not None and not isinstance(stored, bool):
        try:
            months = int(stored)
        except (TypeError, ValueError):
            months = 0
        if months > 0:
            return months
    if chosen_months:
        return chosen_months
    if fixed_days:
        months = (Decimal(fixed_days) / DAYS_PER_MONTH).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
        return max(int(months), 1)
    return None


def periods(limit_period, months):
    """How many times a limit of ``limit_period`` counts over ``months``."""
    span = PERIOD_MONTHS.get(limit_period)
    if span is None or not months:
        return 1
    return math.ceil(months / span)


def _with_components(requested_offerings):
    """``requested_offerings`` with what ``_requestable`` reads of them."""
    return requested_offerings.select_related(
        "offering", "plan__offering"
    ).prefetch_related("offering__components", "plan__offering__components")


def _requestable(requested_offerings):
    """``requestable_components`` of each of ``requested_offerings`` (from
    ``_with_components``), by id, in one query for every plan's prices."""
    priced = defaultdict(set)
    for plan_id, component_type in PlanComponent.objects.filter(
        plan_id__in={ro.plan_id for ro in requested_offerings if ro.plan_id}
    ).values_list("plan_id", "component__type"):
        priced[plan_id].add(component_type)
    return {
        ro.pk: ro.requestable_components(priced=priced[ro.plan_id])
        for ro in requested_offerings
    }


def _limit_periods(requested_offering_ids, requestable=None, rules=None):
    """The limit period of each requestable component, by (requested
    offering id, component type). ``requestable`` and ``rules``, when the
    caller has them, save looking them up."""
    if requestable is None:
        requestable = _requestable(
            list(
                _with_components(
                    models.RequestedOffering.objects.filter(
                        pk__in=set(requested_offering_ids)
                    )
                )
            )
        )
    if rules is None:
        rules = models.CallOfferingComponent.objects.filter(
            requested_offering_id__in=list(requestable)
        )
    result = {
        (ro_id, component_type): effective.limit_period
        for ro_id, components in requestable.items()
        for component_type, effective in components.items()
    }
    # A rule keeps the period the component had when the call took it in.
    for rule in rules:
        if rule.limit_period:
            result[(rule.requested_offering_id, rule.component_type)] = (
                rule.limit_period
            )
    return result


def counted_items(items, award=True, limit_periods=None):
    """Each amount of ``items`` -- awarded resources with ``award``, else
    requested ones -- as it counts against a pool: ``(proposal id, round id,
    requested offering id, component type, amount)``.

    ``limit_periods``, by (requested offering id, component type), saves
    looking them up when the caller already has them."""
    chosen = (
        "proposal__awarded_duration_months"
        if award
        else "proposal__requested_duration_months"
    )
    rows = list(
        items.values_list(
            "proposal_id",
            "proposal__round_id",
            "requested_offering_id",
            "limits",
            "attributes",
            chosen,
            "proposal__round__call__allowed_duration_months",
            "proposal__round__call__fixed_duration_in_days",
        )
    )
    if limit_periods is None:
        limit_periods = _limit_periods(row[2] for row in rows)
    result = []
    for (
        proposal_id,
        round_id,
        requested_offering_id,
        limits,
        attributes,
        chosen_months,
        allowed_months,
        fixed_days,
    ) in rows:
        months = item_months(
            attributes, chosen_months if allowed_months else None, fixed_days
        )
        for component_type, value in (limits or {}).items():
            value = to_decimal(value)
            if value is None:
                continue
            count = periods(
                limit_periods.get((requested_offering_id, component_type)), months
            )
            result.append(
                (
                    proposal_id,
                    round_id,
                    requested_offering_id,
                    component_type,
                    value * count,
                )
            )
    return result


def _approved_decision():
    return Exists(
        models.ProposalWorkflowStepInstance.objects.filter(
            proposal=OuterRef("proposal"),
            is_allocation_decision=True,
            status=WorkflowStepInstanceStatuses.COMPLETED,
            outcome=WorkflowStepOutcomes.APPROVED,
        )
    )


def awarded_items():
    """Awarded resources of proposals that hold their award, less those whose
    order the provider refused: those hand their share back to the pool."""
    return (
        models.AwardedResource.objects.annotate(
            approved=_approved_decision(), refused=models.awarded_order_refused()
        )
        .filter(
            Q(proposal__state=ProposalStates.ACCEPTED)
            | Q(proposal__state=ProposalStates.IN_REVIEW, approved=True)
        )
        .filter(refused=False)
    )


def requested_items():
    """Requested resources of proposals that asked for something."""
    return models.RequestedResource.objects.filter(
        proposal__state__in=REQUESTING_STATES
    )


def undecided_items(held_from=None):
    """Requested resources of proposals still waiting for their allocation
    decision: submitted or in review, with no allocation decision completed,
    whatever its outcome.

    ``held_from``, the proposals whose held decision the reader may not
    learn (as ``call_usage`` takes it), also counts every one of those held
    as undecided: a held approval and a held rejection then read the same,
    as ``call_usage`` counts neither as awarded."""
    decided = Exists(
        models.ProposalWorkflowStepInstance.objects.filter(
            proposal=OuterRef("proposal"),
            is_allocation_decision=True,
            status=WorkflowStepInstanceStatuses.COMPLETED,
        )
    )
    waiting = Q(decided=False)
    if held_from is not None:
        waiting |= Q(proposal__in=held_from.filter(decision_held=True))
    return (
        models.RequestedResource.objects.filter(
            proposal__state__in=ProposalStates.UNDER_EVALUATION
        )
        .annotate(decided=decided)
        .filter(waiting)
    )


def undecided_amounts(items):
    """What ``items`` of ``undecided_items`` ask for, as counted against a
    pool: by (requested offering id, component type), and within by round
    id -- None summing every round."""
    return _sums(counted_items(items, award=False))


# -- the pools ---------------------------------------------------------------------


def _pools():
    return models.CallResourcePool.objects.select_related(
        "offering_component__requested_offering__offering", "round"
    )


def pools_for_round(call_round):
    """The pools a proposal of ``call_round`` draws on: the call-wide ones
    and the round's own, in primary key order."""
    return (
        _pools()
        .filter(offering_component__requested_offering__call_id=call_round.call_id)
        .filter(Q(round__isnull=True) | Q(round=call_round))
        .order_by("pk")
    )


def call_pools(call):
    return _pools().filter(offering_component__requested_offering__call=call)


def _covers(pool, round_id, requested_offering_id, component_type):
    rule = pool.offering_component
    return (
        rule.requested_offering_id == requested_offering_id
        and rule.component_type == component_type
        and (pool.round_id is None or pool.round_id == round_id)
    )


def _pool_items(pool, items):
    """``items`` of the proposals and offering the pool covers."""
    rule = pool.offering_component
    items = items.filter(
        proposal__round__call_id=rule.requested_offering.call_id,
        requested_offering_id=rule.requested_offering_id,
    )
    if pool.round_id is not None:
        items = items.filter(proposal__round_id=pool.round_id)
    return items


def _sum_for(pool, counted):
    total = Decimal(0)
    for (
        _proposal_id,
        round_id,
        requested_offering_id,
        component_type,
        amount,
    ) in counted:
        if _covers(pool, round_id, requested_offering_id, component_type):
            total += amount
    return total


def awarded_so_far(pool, exclude_proposal=None):
    items = _pool_items(pool, awarded_items())
    if exclude_proposal is not None:
        items = items.exclude(proposal=exclude_proposal)
    return _sum_for(pool, counted_items(items, award=True))


def award_amounts(proposal):
    """What the proposal's award draws from the pools, by (requested
    offering id, component type).

    The award once it exists; before the allocation decision has started,
    the request it will be copied from.
    """
    if proposal.awarded_resources_prefilled_at is not None:
        counted = counted_items(
            models.AwardedResource.objects.filter(proposal=proposal), award=True
        )
    else:
        counted = counted_items(
            models.RequestedResource.objects.filter(proposal=proposal), award=False
        )
    amounts = defaultdict(Decimal)
    for (
        _proposal_id,
        _round_id,
        requested_offering_id,
        component_type,
        amount,
    ) in counted:
        amounts[(requested_offering_id, component_type)] += amount
    return dict(amounts)


def pool_label(pool):
    rule = pool.offering_component
    scope = pool.round.name if pool.round_id else _("whole call")
    return f"{rule.requested_offering.offering.name} / {rule.component_type} ({scope})"


def proposal_budget(proposal, pools=None):
    """Each pool the proposal's award draws on: what it offers, has awarded
    to the other proposals, what remains, the proposal's award and whether
    it fits."""
    amounts = award_amounts(proposal)
    if pools is None:
        pools = list(pools_for_round(proposal.round))
    entries = []
    for pool in pools:
        rule = pool.offering_component
        amount = amounts.get((rule.requested_offering_id, rule.component_type))
        awarded = awarded_so_far(pool, exclude_proposal=proposal)
        remaining = pool.offered_total - awarded
        entries.append(
            {
                "pool": pool,
                "offering_name": rule.requested_offering.offering.name,
                "component_type": rule.component_type,
                "round": pool.round,
                "offered_total": pool.offered_total,
                "awarded_so_far": awarded,
                "remaining": remaining,
                "award": amount,
                "fits": amount is None or amount <= remaining,
            }
        )
    return entries


def exceeded(proposal, pools=None):
    """The pools the proposal's award does not fit in, as their entries."""
    return [e for e in proposal_budget(proposal, pools) if not e["fits"]]


def describe_exceeded(entries):
    return "; ".join(
        _("%(pool)s: award %(award)s, remaining %(remaining)s")
        % {
            "pool": pool_label(e["pool"]),
            "award": _format(e["award"]),
            "remaining": _format(e["remaining"]),
        }
        for e in entries
    )


def enforce(proposal, user, override_reason=""):
    """Hold the proposal's award to what remains of the pools it draws on.

    Called when the allocation decision approves the proposal, inside the
    decision's transaction and after the proposal's row lock: the round and
    then the pools are locked here (``locked_pools``), so concurrent
    decisions are checked one after the other against what the earlier ones
    awarded. Raises ``BudgetExceeded`` when the award does not fit and is
    not overridden: in soft mode by anyone deciding with a reason, in hard
    mode only by staff. An override is logged on the proposal's feed for
    the call team.
    """
    pools = locked_pools(proposal.round)
    if not pools:
        return
    over = exceeded(proposal, pools)
    if not over:
        return
    detail = describe_exceeded(over)
    hard = proposal.round.call.budget_enforcement == BudgetEnforcement.HARD
    override_reason = (override_reason or "").strip()
    if override_reason and (not hard or (user is not None and user.is_staff)):
        activity.log_budget_overridden(proposal, detail, override_reason)
        return
    # What remains of a pool tells what the other proposals were awarded:
    # only those who read the call's pools are told the figures.
    shown = (
        f" ({detail})"
        if user is None
        or user == get_system_robot()
        or proposal_permissions.user_can_view_proposal_budget(user, proposal)
        else ""
    )
    if hard:
        raise BudgetExceeded(
            _(
                "The award exceeds what remains of the call's budget%(detail)s. "
                "The call enforces its budget strictly: only staff may override it."
            )
            % {"detail": shown}
        )
    raise BudgetExceeded(
        _(
            "The award exceeds what remains of the call's budget%(detail)s. "
            "Override the budget with a reason to approve it anyway."
        )
        % {"detail": shown}
    )


def locked_pools(call_round):
    """The pools a proposal of ``call_round`` draws on, locked: the round
    first, then the pools in primary key order. The lock every check of an
    award against them takes, inside the deciding transaction and after the
    proposal's row lock."""
    list(
        models.Round.objects.select_for_update()
        .filter(pk=call_round.pk)
        .values_list("pk", flat=True)
    )
    return list(
        models.CallResourcePool.objects.select_for_update(of=("self",))
        .filter(pk__in=pools_for_round(call_round).values("pk"))
        .select_related("offering_component__requested_offering__offering", "round")
        .order_by("pk")
    )


# -- the applicant's request ---------------------------------------------------


def rules_for(requested_offering):
    """The component rules of a call offering, by component type."""
    return {
        rule.component_type: rule
        for rule in models.CallOfferingComponent.objects.filter(
            requested_offering=requested_offering
        )
    }


def bound_limits(requested_offering, limits):
    """``limits`` held to the offering's component rules.

    A fixed rule sets its component to the fixed amount. Returns the limits
    to store; raises ``ValueError`` naming the bounds a value is outside of.
    """
    limits = dict(limits or {})
    errors = []
    for component_type, rule in rules_for(requested_offering).items():
        if rule.is_fixed:
            limits[component_type] = json_number(rule.default_amount)
            continue
        value = to_decimal(limits.get(component_type))
        if value is None:
            continue
        message = _out_of_bounds(rule, value)
        if message:
            errors.append(message)
    if errors:
        raise ValueError(" ".join(errors))
    return limits


def _out_of_bounds(rule, value):
    low, high = rule.min_per_proposal, rule.max_per_proposal
    if (low is not None and value < low) or (high is not None and value > high):
        return _(
            "%(component)s must be between %(low)s and %(high)s per proposal, "
            "not %(value)s."
        ) % {
            "component": rule.component_type,
            "low": _format(low) if low is not None else "0",
            "high": _format(high) if high is not None else "any amount",
            "value": _format(value),
        }
    return None


def request_out_of_bounds(proposal):
    """Messages for the proposal's request totals outside its offerings'
    component rules."""
    totals = defaultdict(Decimal)
    for requested_offering_id, limits in proposal.requestedresource_set.values_list(
        "requested_offering_id", "limits"
    ):
        for component_type, value in (limits or {}).items():
            value = to_decimal(value)
            if value is not None:
                totals[(requested_offering_id, component_type)] += value
    messages = []
    for rule in models.CallOfferingComponent.objects.filter(
        requested_offering__call_id=proposal.round.call_id
    ).select_related("requested_offering__offering"):
        value = totals.get((rule.requested_offering_id, rule.component_type))
        if value is None:
            continue
        message = _out_of_bounds(rule, value)
        if message:
            messages.append(f"{rule.requested_offering.offering.name}: {message}")
    return messages


def template_conflicts(requested_offering, limits, rules=None):
    """Messages for template ``limits`` outside the bounds of the offering's
    component rules."""
    if rules is None:
        rules = rules_for(requested_offering).values()
    messages = []
    for rule in rules:
        value = to_decimal((limits or {}).get(rule.component_type))
        if value is None:
            continue
        message = _out_of_bounds(rule, value)
        if message:
            messages.append(message)
    return messages


def rule_template_conflicts(rule) -> list[str]:
    """Messages for the resource templates of ``rule``'s offering whose
    limits fall outside ``rule``'s bounds, each naming its template."""
    conflicts = []
    for template in models.CallResourceTemplate.objects.filter(
        requested_offering_id=rule.requested_offering_id
    ).order_by("name"):
        for message in template_conflicts(
            rule.requested_offering, template.limits, [rule]
        ):
            conflicts.append(f"{template.name}: {message}")
    return conflicts


def rules_locked(requested_offering) -> bool:
    """Whether the offering's component rules are fixed: once a proposal
    requesting it has been submitted, every applicant is held to the same
    rules."""
    return models.RequestedResource.objects.filter(
        requested_offering=requested_offering,
        proposal__submitted_at__isnull=False,
    ).exists()


def offering_dependents(requested_offering) -> list[str]:
    """What of the call is set on the call offering, by kind: what deleting
    it would delete with it."""
    found = []
    rules = list(
        requested_offering.component_rules.order_by("component_type").values_list(
            "component_type", flat=True
        )
    )
    if rules:
        found.append(_("component rules (%s)") % ", ".join(rules))
    pools = models.CallResourcePool.objects.filter(
        offering_component__requested_offering=requested_offering
    ).count()
    if pools:
        found.append(_("resource pools (%d)") % pools)
    templates = list(
        models.CallResourceTemplate.objects.filter(
            requested_offering=requested_offering
        )
        .order_by("name")
        .values_list("name", flat=True)
    )
    if templates:
        found.append(_("resource templates (%s)") % ", ".join(templates))
    return found


# -- statistics ------------------------------------------------------------------


def call_usage(call, held_from=None):
    """Per component a proposal requests of the call's offerings -- with a
    rule or not -- its unit, and a cell for the whole call and for each
    round: the pool there if any, what it offers, and what was requested,
    awarded and remains. A round's award also draws on the whole call's
    pool, so what remains of a round is never more than what remains of the
    call. Without the awards of ``held_from``, the proposals whose held
    decision the reader may not learn."""
    return calls_usage(models.Call.objects.filter(pk=call.pk), held_from)[call.pk]


def calls_usage(calls, held_from=None):
    """``call_usage`` of each of ``calls``, by call id, in as many queries
    whatever the number of calls."""
    offerings = list(
        _with_components(
            models.RequestedOffering.objects.filter(call__in=calls).exclude(
                state=RequestedOfferingStates.CANCELED
            )
        ).order_by("id")
    )
    requestable = _requestable(offerings)
    rules = {
        (rule.requested_offering_id, rule.component_type): rule
        for rule in models.CallOfferingComponent.objects.filter(
            requested_offering__in=offerings
        )
    }
    pools = {
        (pool.offering_component_id, pool.round_id): pool
        for pool in models.CallResourcePool.objects.filter(
            offering_component__in=rules.values()
        )
    }
    limit_periods = _limit_periods(
        list(requestable), requestable=requestable, rules=rules.values()
    )
    awarded = awarded_items().filter(proposal__round__call__in=calls)
    if held_from is not None:
        awarded = awarded.exclude(proposal__in=held_from.filter(decision_held=True))
    awarded_sums = _sums(
        counted_items(awarded, award=True, limit_periods=limit_periods)
    )
    requested_sums = _sums(
        counted_items(
            requested_items().filter(proposal__round__call__in=calls),
            award=False,
            limit_periods=limit_periods,
        )
    )
    rounds_of = defaultdict(list)
    for call_round in models.Round.objects.filter(call__in=calls).order_by(
        "start_time", "id"
    ):
        rounds_of[call_round.call_id].append(call_round)

    result = {call_id: [] for call_id in calls.values_list("pk", flat=True)}
    for ro in offerings:
        components = requestable[ro.pk]
        # A rule outlives a plan that stopped pricing its component: its
        # pools still count.
        component_types = list(components) + sorted(
            component_type
            for ro_id, component_type in rules
            if ro_id == ro.pk and component_type not in components
        )
        for component_type in component_types:
            effective = components.get(component_type)
            key = (ro.pk, component_type)
            rule = rules.get(key)
            cells = []
            call_remaining = None
            for call_round in [None] + rounds_of[ro.call_id]:
                round_id = call_round.pk if call_round else None
                pool = pools.get((rule.pk, round_id)) if rule else None
                given = awarded_sums[key].get(round_id, Decimal(0))
                remaining = pool.offered_total - given if pool else None
                if call_round is None:
                    call_remaining = remaining
                elif remaining is not None and call_remaining is not None:
                    remaining = min(remaining, call_remaining)
                cells.append(
                    {
                        "round": call_round,
                        "pool": pool,
                        "offered_total": pool.offered_total if pool else None,
                        "requested": requested_sums[key].get(round_id, Decimal(0)),
                        "awarded": given,
                        "remaining": remaining,
                    }
                )
            result[ro.call_id].append(
                {
                    "rule": rule,
                    "requested_offering": ro,
                    "offering_name": ro.offering.name,
                    "component_type": component_type,
                    "component_name": effective.component.name if effective else "",
                    "measured_unit": effective.measured_unit if effective else "",
                    "limit_period": effective.limit_period if effective else "",
                    "cells": cells,
                }
            )
    return result


def _sums(counted):
    """Counted amounts by (requested offering id, component type), and
    within by round id -- None summing every round."""
    sums = defaultdict(lambda: defaultdict(Decimal))
    for (
        _proposal_id,
        round_id,
        requested_offering_id,
        component_type,
        amount,
    ) in counted:
        key = (requested_offering_id, component_type)
        sums[key][round_id] += amount
        sums[key][None] += amount
    return sums
