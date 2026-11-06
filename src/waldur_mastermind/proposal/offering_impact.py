"""What calls have built on an offering, a plan or a component, for the
marketplace to ask before pulling it away (``marketplace.dependents``).

A call depends on an offering it requests or took in, while the call is not
archived. A proposal is *waiting* on it while it still needs the offering to
take an order: in evaluation with a request on it, accepted with an item on
it allocation could not order, or with an extension under way on it. While
any proposal is waiting, only staff may withdraw what it waits on.
"""

import dataclasses
from decimal import Decimal

from django.db.models import Q
from django.utils.translation import gettext as _

from waldur_mastermind.marketplace import dependents as marketplace_dependents
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalRunStates,
    ProposalStates,
    RequestedOfferingStates,
    WorkflowPhases,
)

Changes = marketplace_dependents.Changes

IN_EVALUATION = (ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW)


@dataclasses.dataclass
class CallImpact:
    calls: list[str]
    waiting_proposals: int

    def __bool__(self):
        return bool(self.calls)


def _requested_offerings(offering=None, plan=None):
    requested = models.RequestedOffering.objects.exclude(
        state=RequestedOfferingStates.CANCELED
    ).exclude(call__state=CallStates.ARCHIVED)
    if offering is not None:
        requested = requested.filter(offering=offering)
    if plan is not None:
        requested = requested.filter(offering_id=plan.offering_id)
    return requested


def _waiting(requested, plan=None, component_type=None):
    """Proposals still needing an order on these call offerings."""
    requests = models.RequestedResource.objects.filter(
        requested_offering__in=requested, proposal__state__in=IN_EVALUATION
    )
    awards = models.AwardedResource.objects.filter(requested_offering__in=requested)
    if plan is not None:
        # A request is always on its call offering's plan.
        requests = requests.filter(requested_offering__plan=plan)
        awards = awards.filter(
            Q(plan=plan) | Q(plan__isnull=True, requested_offering__plan=plan)
        )
    if component_type is not None:
        requests = requests.filter(limits__has_key=component_type)
        awards = awards.filter(limits__has_key=component_type)
    unprovisioned = awards.filter(
        proposal__state=ProposalStates.ACCEPTED, resource__isnull=True
    )
    extending = awards.filter(
        proposal__runs__phase=WorkflowPhases.EXTENSION,
        proposal__runs__state=ProposalRunStates.OPEN,
        resource__isnull=False,
    )
    return (
        models.Proposal.objects.filter(
            Q(pk__in=requests.values("proposal_id"))
            | Q(pk__in=unprovisioned.values("proposal_id"))
            | Q(pk__in=extending.values("proposal_id"))
        )
        .distinct()
        .count()
    )


def call_impact(offering=None, plan=None, component=None) -> CallImpact:
    """The calls depending on the object, and how many proposals wait on it."""
    if component is not None:
        offering = component.offering
    requested = _requested_offerings(offering=offering, plan=plan)
    component_type = component.type if component is not None else None
    if plan is not None:
        # The call offerings on the plan, or with award items moved onto it.
        requested = requested.filter(
            Q(plan=plan)
            | Q(
                pk__in=models.AwardedResource.objects.filter(plan=plan).values(
                    "requested_offering_id"
                )
            )
        )
    if component_type is not None:
        requested = requested.filter(
            Q(
                pk__in=models.CallOfferingComponent.objects.filter(
                    component_type=component_type
                ).values("requested_offering_id")
            )
            | Q(
                pk__in=models.RequestedResource.objects.filter(
                    limits__has_key=component_type
                ).values("requested_offering_id")
            )
            | Q(
                pk__in=models.AwardedResource.objects.filter(
                    limits__has_key=component_type
                ).values("requested_offering_id")
            )
        )
    calls = sorted(
        set(requested.values_list("call__name", flat=True)), key=str.casefold
    )
    if not calls:
        return CallImpact([], 0)
    return CallImpact(
        calls, _waiting(requested, plan=plan, component_type=component_type)
    )


def describe(impact: CallImpact) -> str:
    names = ", ".join(impact.calls)
    if impact.waiting_proposals:
        return _(
            "Calls for proposals depend on it: %(calls)s. %(count)s proposals "
            "still need it to take their orders."
        ) % {"calls": names, "count": impact.waiting_proposals}
    return _("Calls for proposals depend on it: %(calls)s.") % {"calls": names}


def provider(change, obj):
    """``marketplace.dependents`` provider for calls."""
    if change in (Changes.OFFERING_WITHDRAWN, Changes.OFFERING_TEAM_DROPPED):
        impact = call_impact(offering=obj)
        if not impact:
            return None
        return marketplace_dependents.Dependent(
            describe(impact), blocks=bool(impact.waiting_proposals)
        )
    if change == Changes.OFFERING_DELETED:
        impact = call_impact(offering=obj)
        # Deleting it would delete the calls' offerings with it.
        return (
            marketplace_dependents.Dependent(describe(impact), blocks_staff=True)
            if impact
            else None
        )
    if change == Changes.PLAN_ARCHIVED:
        impact = call_impact(plan=obj)
        if not impact:
            return None
        return marketplace_dependents.Dependent(
            describe(impact), blocks=bool(impact.waiting_proposals)
        )
    if change == Changes.PLAN_DELETED:
        impact = call_impact(plan=obj)
        return (
            marketplace_dependents.Dependent(describe(impact), blocks_staff=True)
            if impact
            else None
        )
    if change == Changes.COMPONENT_REMOVED:
        impact = call_impact(component=obj)
        # The calls' limits, pools and requests name the component: removing
        # it leaves them bounding nothing, and their orders invalid.
        return (
            marketplace_dependents.Dependent(describe(impact), blocks_staff=True)
            if impact
            else None
        )
    if change == Changes.COMPONENT_CHANGED:
        return _component_changed(obj)
    return None


def _component_changed(candidate):
    """``candidate`` is the component as the change would leave it."""
    current = marketplace_models.OfferingComponent.objects.filter(
        pk=candidate.pk
    ).first()
    if current is None:
        return None
    impact = call_impact(component=current)
    if not impact:
        return None
    if (
        current.billing_type == BillingTypes.LIMIT
        and candidate.billing_type != current.billing_type
    ):
        # Calls request it by the limit its order sets; billed otherwise it
        # can no longer be requested, and their awards no longer ordered.
        return marketplace_dependents.Dependent(
            describe(impact) + " " + _("They request this component by its limit."),
            blocks_staff=True,
        )
    new_max = candidate.max_value
    if new_max and (not current.max_value or new_max < current.max_value):
        over = _amounts_over(current, new_max)
        if over:
            return marketplace_dependents.Dependent(
                describe(impact)
                + " "
                + _("They grant or allow up to %(amount)s, above the new maximum.")
                % {"amount": over},
                blocks_staff=True,
            )
    if candidate.limit_period != current.limit_period:
        # The pools keep the period they were set up with; new rules and
        # requests take the new one.
        return marketplace_dependents.Dependent(describe(impact))
    return None


def _amounts_over(component, new_max):
    """The largest amount of ``component`` a call allows or grants that
    exceeds ``new_max``, or None."""
    requested = _requested_offerings(offering=component.offering)
    amounts = [
        rule.max_per_proposal
        for rule in models.CallOfferingComponent.objects.filter(
            requested_offering__in=requested, component_type=component.type
        )
        if rule.max_per_proposal is not None
    ]
    for model in (models.RequestedResource, models.AwardedResource):
        for limits in model.objects.filter(
            requested_offering__in=requested, limits__has_key=component.type
        ).values_list("limits", flat=True):
            try:
                amounts.append(Decimal(str(limits[component.type])))
            except (ArithmeticError, TypeError, ValueError):
                continue
    over = [amount for amount in amounts if amount > new_max]
    return max(over) if over else None


def rename_component(offering, old_type, new_type):
    """``marketplace.dependents`` renamer: the calls' rules, requests,
    awards, templates and extension requests on the offering follow a
    component's new type."""
    requested = models.RequestedOffering.objects.filter(offering=offering)
    models.CallOfferingComponent.objects.filter(
        requested_offering__in=requested, component_type=old_type
    ).update(component_type=new_type)
    for model in (
        models.RequestedResource,
        models.AwardedResource,
        models.CallResourceTemplate,
    ):
        for item in model.objects.filter(
            requested_offering__in=requested, limits__has_key=old_type
        ):
            item.limits[new_type] = item.limits.pop(old_type)
            item.save(update_fields=["limits"])
    for run in models.ProposalRun.objects.filter(
        proposal__awarded_resources__requested_offering__in=requested
    ).distinct():
        changed = False
        for field in ("requested_limits", "granted_limits"):
            for limits in (getattr(run, field) or {}).values():
                if isinstance(limits, dict) and old_type in limits:
                    limits[new_type] = limits.pop(old_type)
                    changed = True
        if changed:
            run.save(update_fields=["requested_limits", "granted_limits"])
