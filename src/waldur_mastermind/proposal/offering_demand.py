"""How much the proposals of calls ask of a provider's offerings.

One row per offering, call, round and group of proposal states: pending
(submitted or in review), accepted, and closed (rejected or cancelled). An
applicant's draft is not an application yet and is left out, as everywhere
on the provider side (``provider_calls.SHOWN_PROPOSAL_EXCLUDED_STATES``).

A pending or accepted row sums, per component the call offering's plan
bills by its limit, what the group's requested resources ask for; an
accepted row also sums what their awards grant. Amounts are in the
component's measured unit and per its limit period, as limits are stored:
a component limited per month sums monthly amounts. A closed row only
counts its proposals.

Where the call pools a component of the offering, the row carries the
pool's figures for the whole call and for its round, counted as
``budgets`` counts them: over the whole length of each award, and from the
awards held, whatever proposal they belong to.

Only the provider side reads these numbers: staff and support see every
offering; anyone else the offerings of the organisations they own and those
they hold an offering role on (``contributions.offering_ids_with_role_of``,
the offerings whose part of a proposal a provider reads). A call's managers
read nothing here through the call; they have the call's own pools view.
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Q

from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.managers import get_connected_customers
from waldur_mastermind.marketplace import billing_mode
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.proposal import budgets, contributions, models
from waldur_mastermind.proposal.enums import ProposalStates


class DemandGroups:
    """The proposal states a row of demand groups together."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    CLOSED = "closed"

    CHOICES = (
        (PENDING, "Pending: submitted or in review"),
        (ACCEPTED, "Accepted"),
        (CLOSED, "Closed: rejected or cancelled"),
    )

    STATES = {
        PENDING: (ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW),
        ACCEPTED: (ProposalStates.ACCEPTED,),
        CLOSED: (ProposalStates.REJECTED, ProposalStates.CANCELED),
    }

    ORDER = (PENDING, ACCEPTED, CLOSED)


GROUP_OF_STATE = {
    state: group for group, states in DemandGroups.STATES.items() for state in states
}


def provider_offerings(user):
    """The offerings whose demand ``user`` reads: every one for staff and
    support; else those of the organisations they own and those they hold
    an offering role on."""
    offerings = marketplace_models.Offering.objects.all()
    if user.is_staff or user.is_support:
        return offerings
    return offerings.filter(
        Q(customer_id__in=get_connected_customers(user, RoleEnum.CUSTOMER_OWNER))
        | Q(id__in=contributions.offering_ids_with_role_of(user))
    )


def _requestable(requested_offering):
    """``RequestedOffering.requestable_components`` read from the prefetched
    offering components and plan components, without a query per call
    offering."""
    plan = requested_offering.plan
    resolved = billing_mode.ResolvedPlan(requested_offering.offering, plan)
    priced = (
        {plan_component.component.type for plan_component in plan.components.all()}
        if plan
        else None
    )
    return {
        component_type: effective
        for component_type, effective in resolved.components.items()
        if effective.billing_type == BillingTypes.LIMIT
        and (priced is None or component_type in priced)
    }


def _add_limits(totals, limits):
    for component_type, value in (limits or {}).items():
        value = budgets.to_decimal(value)
        if value is not None:
            totals[component_type] += value


class _Cell:
    def __init__(self):
        self.proposals = set()
        self.requested = defaultdict(Decimal)
        self.awarded = defaultdict(Decimal)


def _pool_figures(requested_offerings, requestable):
    """The pools on the call offerings, as ``{(requested offering id,
    component type, round id or None): {offered, awarded, remaining}}``."""
    pools = list(
        models.CallResourcePool.objects.filter(
            offering_component__requested_offering__in=list(requested_offerings)
        ).select_related("offering_component")
    )
    if not pools:
        return {}
    pooled = {pool.offering_component.requested_offering_id for pool in pools}
    limit_periods = {
        (requested_offering_id, component_type): effective.limit_period
        for requested_offering_id in pooled
        for component_type, effective in requestable[requested_offering_id].items()
    }
    awarded = defaultdict(Decimal)
    for (
        _proposal_id,
        round_id,
        requested_offering_id,
        component_type,
        amount,
    ) in budgets.counted_items(
        budgets.awarded_items().filter(requested_offering_id__in=pooled),
        award=True,
        limit_periods=limit_periods,
    ):
        awarded[(requested_offering_id, component_type, round_id)] += amount
        awarded[(requested_offering_id, component_type, None)] += amount
    figures = {}
    for pool in pools:
        rule = pool.offering_component
        key = (rule.requested_offering_id, rule.component_type, pool.round_id)
        given = awarded.get(key, Decimal(0))
        figures[key] = {
            "offered": pool.offered_total,
            "awarded": given,
            "remaining": pool.offered_total - given,
        }
    return figures


def demand(
    user,
    offering_uuid=None,
    call_uuid=None,
    round_uuid=None,
    provider_uuid=None,
    groups=None,
):
    """The demand rows ``user`` reads, ordered by offering, call, round and
    group.

    A fixed number of queries, however many proposals ask: the call
    offerings with their components, the requested and awarded resources as
    values, the rounds, and the pools with the awards counted against them.
    """
    requested_offerings = models.RequestedOffering.objects.filter(
        offering__in=provider_offerings(user)
    )
    if offering_uuid:
        requested_offerings = requested_offerings.filter(offering__uuid=offering_uuid)
    if call_uuid:
        requested_offerings = requested_offerings.filter(call__uuid=call_uuid)
    if provider_uuid:
        requested_offerings = requested_offerings.filter(
            offering__customer__uuid=provider_uuid
        )
    by_id = {
        requested_offering.pk: requested_offering
        for requested_offering in requested_offerings.select_related(
            "offering__customer", "call", "plan"
        ).prefetch_related("offering__components", "plan__components__component")
    }
    if not by_id:
        return []

    groups = [group for group in DemandGroups.ORDER if not groups or group in groups]
    states = [state for group in groups for state in DemandGroups.STATES[group]]

    requested = models.RequestedResource.objects.filter(
        requested_offering_id__in=list(by_id), proposal__state__in=states
    )
    if round_uuid:
        requested = requested.filter(proposal__round__uuid=round_uuid)
    cells = defaultdict(_Cell)
    for (
        proposal_id,
        state,
        round_id,
        requested_offering_id,
        limits,
    ) in requested.values_list(
        "proposal_id",
        "proposal__state",
        "proposal__round_id",
        "requested_offering_id",
        "limits",
    ):
        group = GROUP_OF_STATE[state]
        cell = cells[(requested_offering_id, round_id, group)]
        cell.proposals.add(proposal_id)
        if group != DemandGroups.CLOSED:
            _add_limits(cell.requested, limits)

    if DemandGroups.ACCEPTED in groups:
        # What the award grants on the offering, which may differ from the
        # request: an item moved here from another offering counts here.
        awarded = models.AwardedResource.objects.filter(
            requested_offering_id__in=list(by_id),
            proposal__state=ProposalStates.ACCEPTED,
        )
        if round_uuid:
            awarded = awarded.filter(proposal__round__uuid=round_uuid)
        for proposal_id, round_id, requested_offering_id, limits in awarded.values_list(
            "proposal_id",
            "proposal__round_id",
            "requested_offering_id",
            "limits",
        ):
            cell = cells[(requested_offering_id, round_id, DemandGroups.ACCEPTED)]
            cell.proposals.add(proposal_id)
            _add_limits(cell.awarded, limits)

    if not cells:
        return []

    rounds = models.Round.objects.in_bulk({round_id for _, round_id, _ in cells})
    requestable = {pk: _requestable(ro) for pk, ro in by_id.items()}
    pools = _pool_figures(by_id, requestable)

    def sort_key(key):
        requested_offering_id, round_id, group = key
        requested_offering = by_id[requested_offering_id]
        call_round = rounds[round_id]
        return (
            requested_offering.offering.name.lower(),
            requested_offering.offering_id,
            requested_offering.call.name.lower(),
            requested_offering.call_id,
            call_round.start_time,
            round_id,
            DemandGroups.ORDER.index(group),
        )

    rows = []
    for key in sorted(cells, key=sort_key):
        requested_offering_id, round_id, group = key
        requested_offering = by_id[requested_offering_id]
        cell = cells[key]
        rows.append(
            {
                "requested_offering": requested_offering,
                "offering": requested_offering.offering,
                "provider": requested_offering.offering.customer,
                "call": requested_offering.call,
                "round": rounds[round_id],
                "state_group": group,
                "proposals_count": len(cell.proposals),
                "components": []
                if group == DemandGroups.CLOSED
                else _components(
                    requested_offering,
                    requestable[requested_offering_id],
                    cell,
                    group,
                    round_id,
                    pools,
                ),
            }
        )
    return rows


def _components(requested_offering, requestable, cell, group, round_id, pools):
    """A row's components: those the call offering's plan bills by limit,
    then any other its proposals ask or are awarded."""
    own = {
        component.type: component
        for component in requested_offering.offering.components.all()
    }
    extra = sorted((set(cell.requested) | set(cell.awarded)) - set(requestable))
    result = []
    for component_type in list(requestable) + extra:
        effective = requestable.get(component_type)
        component = effective.component if effective else own.get(component_type)
        result.append(
            {
                "type": component_type,
                "name": component.name if component else "",
                "measured_unit": (
                    effective.measured_unit
                    if effective
                    else (component.measured_unit if component else "")
                ),
                "limit_period": (
                    effective.limit_period
                    if effective
                    else (component.limit_period if component else "")
                )
                or "",
                "requested": cell.requested.get(component_type, Decimal(0)),
                "awarded": (
                    cell.awarded.get(component_type, Decimal(0))
                    if group == DemandGroups.ACCEPTED
                    else None
                ),
                "call_pool": pools.get((requested_offering.pk, component_type, None)),
                "round_pool": pools.get(
                    (requested_offering.pk, component_type, round_id)
                ),
            }
        )
    return result
