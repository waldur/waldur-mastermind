"""A call's panels: which panel a proposal goes to, and who sits on it.

A call may define panels (``CallPanel``), each covering some science domains
and sub-domains, with a chair and members drawn from the call's panel
members. A proposal is routed to one when its evaluation starts
(``route``), and evaluator roles eligible from ``panel:chair`` or
``panel:member`` are held by the chair or the members of the proposal's
panel only.

This module is the one place that reads ``Proposal.panel`` and a panel's
people: ``panel_of`` and ``chair_of`` answer "which panel, and whose chair"
for anything grouping a proposal's work by panel. Panel membership never
widens access: who sits on a panel is always narrowed to who holds the panel
member role on the call now.
"""

import logging

from django.db import transaction
from django.db.models import Q
from rest_framework import serializers

from waldur_core.core.models import User
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    ProposalRoleEligibility,
    ProposalStates,
    StepContributionStatuses,
)

logger = logging.getLogger(__name__)

# A panel is changed only while the proposal is being evaluated or waits for
# its evaluation to start.
PANEL_CHANGEABLE_STATES = (ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW)


def is_panel_based(role) -> bool:
    """Whether who may hold ``role`` depends on the proposal's panel."""
    return role.eligible_from in ProposalRoleEligibility.PANEL_BASED


# -- reading ------------------------------------------------------------------


def call_panel_members(call):
    """The call's panel members now: the only people a panel counts."""
    return get_users(call, role_name=RoleEnum.CALL_PANEL_MEMBER).filter(is_active=True)


def members_of(panel):
    """The panel's members who still hold the call's panel member role."""
    return call_panel_members(panel.call).filter(
        id__in=panel.members.values_list("id", flat=True)
    )


def chair_of_panel(panel):
    """The panel's chair while they still hold the call's panel member role,
    else None."""
    if panel is None or panel.chair_id is None:
        return None
    return call_panel_members(panel.call).filter(pk=panel.chair_id).first()


def panel_of(proposal):
    """The panel ``proposal`` is routed to, or None."""
    return proposal.panel if proposal.panel_id else None


def chair_of(proposal):
    """The chair of ``proposal``'s panel, or None.

    The chair as the panel names them, provided they still hold the call's
    panel member role. Whether they may act on this proposal -- a conflict
    of interest, the applicant team -- is the caller's to check, as for any
    evaluator.
    """
    return chair_of_panel(panel_of(proposal))


def eligible_users(role, proposal=None):
    """Who may hold the panel-based ``role``: the chair or the members of
    ``proposal``'s panel. Nobody without a proposal, or for a proposal with
    no panel."""
    if proposal is None or proposal.panel_id is None:
        return User.objects.none()
    panel = proposal.panel
    if role.eligible_from == ProposalRoleEligibility.PANEL_CHAIR:
        if panel.chair_id is None:
            return User.objects.none()
        return call_panel_members(panel.call).filter(pk=panel.chair_id)
    return members_of(panel)


def smallest_panel(call, eligibility):
    """``(panel, count)`` of the call's panel with the fewest people able to
    hold a role of ``eligibility``; ``(None, 0)`` when the call has none."""
    smallest, fewest = None, None
    member_ids = set(call_panel_members(call).values_list("id", flat=True))
    for panel in call.panels.prefetch_related("members"):
        if eligibility == ProposalRoleEligibility.PANEL_CHAIR:
            count = int(panel.chair_id in member_ids)
        else:
            count = len(member_ids & {member.pk for member in panel.members.all()})
        if fewest is None or count < fewest:
            smallest, fewest = panel, count
    return smallest, fewest or 0


def panel_for(call, sub_domain):
    """The panel of ``call`` covering ``sub_domain``: the panel mapping the
    sub-domain itself, else the panel mapping its domain, else None."""
    if sub_domain is None:
        return None
    panels = models.CallPanel.objects.filter(call=call).order_by("order", "id")
    return (
        panels.filter(science_sub_domains=sub_domain).first()
        or panels.filter(science_domains=sub_domain.domain_id).first()
    )


# -- routing and moving -------------------------------------------------------


def route(proposal):
    """Send ``proposal`` to the panel covering its science sub-domain, unless
    it already has one. Called when its evaluation starts, under the
    proposal's row lock. Returns the panel it was sent to now, or None when
    it already had one or none covers it."""
    if proposal.panel_id is not None:
        return None
    panel = panel_for(proposal.round.call, proposal.science_sub_domain)
    if panel is None:
        return None
    proposal.panel = panel
    proposal.save(update_fields=["panel"])
    return panel


def held_panel_roles(proposal):
    """The proposal's assignments, not revoked, of roles held through its
    panel: while there are any, the panel cannot change."""
    return models.ProposalRoleAssignment.objects.filter(
        proposal=proposal,
        revoked_at__isnull=True,
        role__eligible_from__in=ProposalRoleEligibility.PANEL_BASED,
    )


def acted_on(user_id, proposal) -> bool:
    """Whether the user has done anything on ``proposal`` beyond confirming
    having no conflict of interest: a contribution taken up or submitted
    (their own or one handed to them), a step completed, a review, an
    evaluator assigned or changes requested."""
    if (
        models.StepContribution.objects.filter(
            Q(contributor_id=user_id) | Q(delegate_id=user_id),
            proposal_step__proposal=proposal,
        )
        .filter(
            Q(submitted_at__isnull=False)
            | Q(
                status__in=(
                    StepContributionStatuses.ACCEPTED,
                    StepContributionStatuses.SUBMITTED,
                )
            )
        )
        .exists()
    ):
        return True
    if proposal.workflow_step_instances.filter(completed_by_id=user_id).exists():
        return True
    if models.Review.objects.filter(proposal=proposal, reviewer_id=user_id).exists():
        return True
    if models.ProposalRoleAssignment.objects.filter(
        proposal=proposal, assigned_by_id=user_id
    ).exists():
        return True
    return models.ProposalChangeRequest.objects.filter(
        proposal=proposal, requested_by_id=user_id
    ).exists()


def why_not_movable(assignment) -> str | None:
    """Why ``assignment``, a panel-based role on its proposal, keeps the
    proposal on its panel, or None when moving the proposal may revoke it.

    Only the panel chair the workflow assigned itself goes with a move, and
    only while they have done nothing on the proposal yet.
    """
    role, user = assignment.role, assignment.user
    name = user.full_name or user.username
    if (
        role.eligible_from != ProposalRoleEligibility.PANEL_CHAIR
        or assignment.assigned_by_id is not None
        or assignment.assigned_in_id is None
    ):
        return (
            f"{name} was assigned the role '{role.name}' by hand: revoke it "
            "before moving the proposal to another panel."
        )
    if acted_on(user.pk, assignment.proposal):
        return (
            f"{name} has already worked on this proposal as '{role.name}': "
            "revoke the role before moving the proposal to another panel."
        )
    return None


@transaction.atomic
def set_panel(proposal, panel, user):
    """Move ``proposal`` to ``panel`` (None takes it off any panel).

    Refused for a panel of another call (answered as one that does not
    exist), outside evaluation, and while holders of the proposal's
    panel-based evaluator roles remain: they were picked from the old panel,
    and would keep their access -- except for the panel chair the workflow
    assigned itself who has done nothing on the proposal yet
    (``why_not_movable``), whose assignment the caller revokes. Returns
    ``(proposal, previous panel, assignments to revoke)``, the proposal as
    locked and changed; the previous panel is the current one when nothing
    changed.
    """
    proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
        pk=proposal.pk
    )
    if panel is not None and panel.call_id != proposal.round.call_id:
        raise serializers.ValidationError(
            {"panel": "The call has no such panel."}, code="does_not_exist"
        )
    if proposal.state not in PANEL_CHANGEABLE_STATES:
        raise serializers.ValidationError(
            "A proposal changes panel only once it is submitted and while it "
            "is evaluated."
        )
    if proposal.panel_id == (panel.pk if panel else None):
        return proposal, panel, []
    held = list(
        held_panel_roles(proposal)
        .select_related("role", "user", "proposal")
        .order_by("id")
    )
    for assignment in held:
        reason = why_not_movable(assignment)
        if reason:
            raise serializers.ValidationError(reason)
    previous = panel_of(proposal)
    proposal.panel = panel
    proposal.save(update_fields=["panel"])
    logger.info(
        "Proposal %s moved from panel %s to panel %s by user %s",
        proposal.uuid.hex,
        previous.uuid.hex if previous else None,
        panel.uuid.hex if panel else None,
        user.uuid.hex if user else None,
    )
    return proposal, previous, held


def remove_from_call_panels(call, user):
    """Take ``user`` off every panel of ``call`` and clear them as chair:
    they no longer hold the call's panel member role. Under the panels' row
    locks, so a panel being edited meanwhile cannot put them back unseen."""
    panels = list(
        models.CallPanel.objects.select_for_update().filter(call=call).order_by("id")
    )
    if not panels:
        return
    models.CallPanel.members.through.objects.filter(
        callpanel_id__in=[panel.pk for panel in panels], user_id=user.pk
    ).delete()
    models.CallPanel.objects.filter(
        pk__in=[panel.pk for panel in panels], chair_id=user.pk
    ).update(chair=None)
