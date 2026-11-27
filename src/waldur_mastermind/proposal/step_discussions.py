"""Tracked discussions of workflow steps.

A call step with ``has_discussion`` lets the people working on it talk it
through in an end-to-end encrypted Matrix room kept with the step's record
(``models.StepDiscussion``): one per proposal's step, or for a step run once
per round one per round, and one per panel on a call with panels, so that
panels do not read each other's deliberations.

- Who belongs in the room (``people``) is registered with matrix_chat as the
  discussion's members (``apps.ProposalConfig.ready``). Its member sync keeps
  the room in line, and is asked again (``resync_*``) whenever who belongs in
  it may have changed.
- One of its participants opens it (``open_discussion``), which creates the
  room the first time and returns the same discussion afterwards.
- It closes when its step ends (``close_ended``, ``close_finished_round``,
  ``close_proposal``): who took part is kept on the record, and the room's
  history is exported before the room is archived.

While Matrix chat is disabled nothing opens and nothing is synced or closed on
the homeserver; the records stay readable, and close without touching a room.
"""

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.urls import reverse
from django.utils import timezone
from rest_framework import exceptions, status

from waldur_core.core.auth_utils import is_pat_auth
from waldur_core.core.models import User
from waldur_core.permissions import models as permissions_models
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.pat_filtering import PATScopeListFilter
from waldur_core.permissions.utils import check_pat_support_scope, get_users
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.matrix_chat import matrix_client, room_provisioning
from waldur_mastermind.matrix_chat import models as matrix_models
from waldur_mastermind.matrix_chat import tasks as matrix_tasks
from waldur_mastermind.proposal import (
    archive,
    contributions,
    dry_run_context,
    models,
    my_tasks,
    notification_rules,
    panels,
    proposal_roles,
    rankings,
    round_reports,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    CallStates,
    ContributorSources,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    StepContributionStatuses,
    StepDiscussionStates,
    WorkflowStepInstanceStatuses,
    parse_contributor_source,
)

logger = logging.getLogger(__name__)

# A moderator of the room; everyone else is a plain member.
MODERATOR = 50

# Contributions whose holders are still part of the step's work.
LIVE_CONTRIBUTIONS = (
    *StepContributionStatuses.OPEN,
    StepContributionStatuses.SUBMITTED,
)
UNDER_EVALUATION = (ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW)
# A proposal leaving these has left every step it stood at.
ENDED_PROPOSAL_STATES = (ProposalStates.REJECTED, ProposalStates.CANCELED)

ROOM_CLOSING_REASON = "the end of the step"

NOT_OPENED = StepDiscussionStates.NOT_OPENED
OPEN = StepDiscussionStates.OPEN
CLOSED = StepDiscussionStates.CLOSED

# Who list and retrieve show. Change it with visible_to.
SHOWS = (
    "Users with the staff flag see every discussion, and those with the "
    "support flag (with a personal access token, its support scope) every "
    "one they are not kept away from. Others see those they take part in -- "
    "an open one whose room they are in, a closed one they took part in; "
    "with a personal access token only with PROPOSAL.LIST and within its "
    "bindings to calls and proposals -- and those of calls whose "
    "configuration they may "
    "update (CALL.UPDATE on the call or its managing organisation; a personal "
    "access token only where its scopes and bindings allow CALL.UPDATE). "
    "Nobody but staff sees the discussion of a proposal they are on the "
    "applicant team of, wrote, or have a conflict of interest on record "
    "with, nor a round discussion of a round -- on a call with panels, of "
    "the panel -- with any such proposal."
)
# Who may open one. Change it with check_open.
OPENS = (
    "A participant of the step's discussion as they stand now: a holder of "
    "an open or submitted contribution to the step who still holds the "
    "standing it was given through, its completer, the call's managers when "
    "the step includes them, the panel's members when it includes them. "
    "Never the applicant team, the author or anyone with a conflict of "
    "interest on record with the proposal; for a round discussion, with any "
    "proposal of the round on the panel. Anyone else is refused alike, "
    "whatever state the step is in. A personal access token opens only a "
    "discussion it could read: with PROPOSAL.LIST within its bindings to "
    "the call or proposal, or CALL.UPDATE on the call."
)
NOT_TAKING_PART = "Only the people working on the step take part in its discussion."


MATRIX_DISABLED = (
    "Matrix chat is disabled, so no discussion opens. Discussions already "
    "held stay readable."
)


class DiscussionConflict(exceptions.APIException):
    """The discussion cannot be opened in the state its step is in."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"
    default_detail = "The step's discussion cannot be opened now."


# =============================================================================
# Who takes part
# =============================================================================


def _role_slot_holders(instance, user_ids) -> set[int]:
    """Of ``user_ids``, those who still contribute to a step of role holders
    -- an evaluator role or a call role -- through it: a contribution they
    submitted stays on record once the role is gone, but they no longer take
    part (contributions.may_contribute)."""
    call_step = instance.call_step
    if call_step.contributor_source_kind == ContributorSources.PROPOSAL_ROLE:
        # Every holder of the role still works on a lead-only step, as
        # contributions.holds_step_work has it.
        return set(
            contributions.proposal_role_holders(
                instance.proposal, call_step.contributor_source
            )
            .filter(pk__in=user_ids)
            .values_list("id", flat=True)
        )
    return {
        user.pk
        for user in User.all_objects.filter(pk__in=user_ids)
        if contributions.may_contribute(user, instance)
    }


def _contributor_ids(instance) -> set[int]:
    """The people holding an open or submitted contribution to the step who
    still hold what it was theirs through: on a step of role holders the
    role, on an offering's slot a role on the offering; a slot handed on is
    its delegate's while they may still take it over
    (contributions.effective_delegate_id). An offering's slot nobody has
    taken yet is its offering managers'."""
    slots = list(
        instance.contributions.filter(status__in=LIVE_CONTRIBUTIONS).select_related(
            "delegate", "requested_offering"
        )
    )
    ids, untaken, person_ids = set(), set(), set()
    offering_ids = {
        s.requested_offering.offering_id for s in slots if s.requested_offering_id
    }
    teams: dict[int, set[int]] = {}
    managers: dict[int, set[int]] = {}
    if offering_ids:
        for (
            offering_id,
            user_id,
            role_name,
        ) in permissions_models.UserRole.objects.filter(
            is_active=True,
            content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
            object_id__in=offering_ids,
        ).values_list("object_id", "user_id", "role__name"):
            teams.setdefault(offering_id, set()).add(user_id)
            if role_name == RoleEnum.OFFERING_MANAGER:
                managers.setdefault(offering_id, set()).add(user_id)
    for slot in slots:
        slot.proposal_step = instance
        if slot.delegate_id and (delegate := contributions.effective_delegate_id(slot)):
            ids.add(delegate)
        if slot.requested_offering_id:
            offering_id = slot.requested_offering.offering_id
            if slot.contributor_id is None:
                untaken.add(offering_id)
            elif slot.contributor_id in teams.get(offering_id, ()):
                ids.add(slot.contributor_id)
        elif slot.contributor_id:
            person_ids.add(slot.contributor_id)
    for offering_id in untaken:
        ids |= managers.get(offering_id, set())
    if person_ids and (
        instance.call_step.contributor_source_kind in contributions.ROLE_SOURCES
    ):
        person_ids = _role_slot_holders(instance, person_ids)
    return ids | person_ids


def _completer_ids(call_step, proposal) -> set[int]:
    return set(
        notification_rules.step_completer_users(call_step, proposal).values_list(
            "id", flat=True
        )
    )


def _memo(cache, key, compute):
    """``compute()``, kept in ``cache`` under ``key`` when a cache is given:
    the rows of one response ask the same questions again and again."""
    if cache is None:
        return compute()
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def _call_manager_ids(call, cache=None) -> set[int]:
    return _memo(
        cache,
        ("call_managers", call.pk),
        lambda: set(call.call_managers.values_list("id", flat=True)),
    )


def _panel_people(call, cache=None):
    """``(the call's panel members, {panel id: its members})``: those holding
    the call's panel member role now, the only people a panel counts
    (panels.members_of), and whom each panel names, in two queries for all
    of the call's panels."""

    def compute():
        role_ids = set(panels.call_panel_members(call).values_list("id", flat=True))
        members: dict[int, set[int]] = {}
        for panel_id, user_id in models.CallPanel.members.through.objects.filter(
            callpanel__call=call
        ).values_list("callpanel_id", "user_id"):
            members.setdefault(panel_id, set()).add(user_id)
        return role_ids, members

    return _memo(cache, ("panel_people", call.pk), compute)


def _panel_chair_id(call, panel, cache=None):
    """The chair of ``panel`` while they hold the call's panel member role
    (panels.chair_of_panel), or None."""
    role_ids, _ = _panel_people(call, cache)
    return panel.chair_id if panel.chair_id in role_ids else None


def _panel_member_ids(call, panel, cache=None) -> set[int]:
    """The members of ``panel``, its chair included; without a panel, the
    call's panel members when the call has no panels, else nobody."""
    if panel is None and _has_panels(call, cache):
        return set()
    role_ids, members = _panel_people(call, cache)
    if panel is None:
        return set(role_ids)
    ids = members.get(panel.pk, set()) & role_ids
    if (chair_id := _panel_chair_id(call, panel, cache)) is not None:
        ids.add(chair_id)
    return ids


def _chair_ids(call, panel, proposal=None, cache=None) -> set[int]:
    """The chair of ``panel``; without one, the call's panel chair and
    vice-chair."""
    if panel is not None:
        chair_id = _panel_chair_id(call, panel, cache)
        return {chair_id} if chair_id is not None else set()
    return _memo(
        cache,
        ("call_chairs", call.pk, proposal.pk if proposal is not None else None),
        lambda: proposal_permissions.panel_chair_ids(call, proposal),
    )


def _proposal_step_people(instance, cache=None):
    call_step = instance.call_step
    proposal = instance.proposal
    call = proposal.round.call
    panel = panels.panel_of(proposal)
    completers = _completer_ids(call_step, proposal)
    ids = _contributor_ids(instance) | completers
    if call_step.discussion_call_managers:
        ids |= _call_manager_ids(call, cache)
    if call_step.discussion_panel_members:
        ids |= _panel_member_ids(call, panel, cache)
    # A conflict of interest with the proposal, or a place on its team or
    # its authorship -- conflicts of their own -- keeps anyone out, whatever
    # brought them in: the round-reading rule (rankings).
    ids -= _memo(
        cache,
        ("conflicted_with", proposal.pk),
        lambda: rankings.conflicted_user_ids(
            models.Proposal.objects.filter(pk=proposal.pk)
        ),
    )
    moderators = (completers | _chair_ids(call, panel, proposal, cache)) & ids
    return ids, moderators


def round_proposals(round_id, panel):
    """Every proposal of the round, whatever its state, on ``panel`` when
    given: those whose teams, authors and conflicted people a round
    discussion keeps out."""
    proposals = models.Proposal.objects.filter(round_id=round_id)
    if panel is not None:
        proposals = proposals.filter(panel=panel)
    return proposals


def _covered_ids(discussion_id):
    """The ids of the proposals a saved discussion has covered."""
    return models.StepDiscussion.covered_proposals.through.objects.filter(
        stepdiscussion_id=discussion_id
    ).values("proposal_id")


def _round_exclusion(round_id, panel, discussion_id=None):
    """The proposals a round discussion keeps the teams, authors and
    conflicted people of out: those of the round on its panel now, and,
    once saved, every one it covered before (``covered_proposals``), so that
    a proposal moved to another panel keeps its people out of this one."""
    proposals = round_proposals(round_id, panel)
    if discussion_id is None:
        return proposals
    return models.Proposal.objects.filter(
        Q(id__in=proposals.order_by().values("id"))
        | Q(id__in=_covered_ids(discussion_id))
    )


def covered_proposals(discussion):
    """The proposals ``discussion`` is about, now or before: a proposal's
    step its proposal, a round discussion ``_round_exclusion``."""
    if discussion.proposal_step_id:
        return models.Proposal.objects.filter(pk=discussion.proposal_step.proposal_id)
    return _round_exclusion(
        discussion.round_step.round_id, discussion.panel, discussion.pk
    )


def cover(discussion):
    """Record the proposals ``discussion`` covers now; never removed."""
    if discussion.proposal_step_id:
        ids = [discussion.proposal_step.proposal_id]
    else:
        ids = list(
            round_proposals(discussion.round_step.round_id, discussion.panel)
            .order_by()
            .values_list("id", flat=True)
        )
    through = models.StepDiscussion.covered_proposals.through
    through.objects.bulk_create(
        [through(stepdiscussion_id=discussion.pk, proposal_id=pk) for pk in ids],
        ignore_conflicts=True,
    )


def cover_proposal(proposal):
    """``proposal`` joined a panel, or its round: the open round discussions
    of that panel -- of the round, on a call without panels -- cover it from
    now on."""
    discussions = _open_discussions().filter(round_step__round_id=proposal.round_id)
    if proposal.panel_id:
        discussions = discussions.filter(panel_id=proposal.panel_id)
    else:
        discussions = discussions.filter(panel__isnull=True)
    through = models.StepDiscussion.covered_proposals.through
    through.objects.bulk_create(
        [
            through(stepdiscussion_id=pk, proposal_id=proposal.pk)
            for pk in discussions.values_list("id", flat=True)
        ],
        ignore_conflicts=True,
    )


def round_instances(round_step, panel):
    """The proposals' steps a round discussion covers: those of the round at
    the round step, or done with it, on the discussion's panel."""
    return _round_instances(round_step.round_id, round_step.call_step_id, panel)


def _round_instances(round_id, call_step_id, panel):
    instances = models.ProposalWorkflowStepInstance.objects.filter(
        proposal__round_id=round_id,
        call_step_id=call_step_id,
        run__isnull=True,
        status__in=(
            WorkflowStepInstanceStatuses.ACTIVE,
            WorkflowStepInstanceStatuses.COMPLETED,
        ),
    ).select_related("call_step", "proposal__round__call")
    if panel is not None:
        instances = instances.filter(proposal__panel=panel)
    return instances


def _round_step_people(round_step, panel, discussion_id=None, cache=None):
    return _round_people(
        round_step.round, round_step.call_step, panel, discussion_id, cache
    )


def _round_covered(call_round, call_step, panel, cache=None) -> list[int]:
    """The proposals ``_round_instances`` covers; with a cache, those of
    every panel of the round step at once."""
    if cache is None:
        return list(
            _round_instances(call_round.pk, call_step.pk, panel).values_list(
                "proposal_id", flat=True
            )
        )

    def compute():
        by_panel: dict[int | None, list[int]] = {}
        for proposal_id, panel_id in (
            _round_instances(call_round.pk, call_step.pk, None)
            .order_by()
            .values_list("proposal_id", "proposal__panel_id")
        ):
            by_panel.setdefault(panel_id, []).append(proposal_id)
        return by_panel

    by_panel = _memo(cache, ("round_covered", call_round.pk, call_step.pk), compute)
    if panel is None:
        return [pk for ids in by_panel.values() for pk in ids]
    return list(by_panel.get(panel.pk, ()))


def _round_conflicted_ids(round_id, panel, discussion_id=None, cache=None):
    """``rankings.conflicted_user_ids`` of ``_round_exclusion``; with a
    cache, from the round's proposals' conflicts, loaded once."""
    if cache is None:
        return rankings.conflicted_user_ids(
            _round_exclusion(round_id, panel, discussion_id)
        )

    def proposals_of_round():
        proposals = models.Proposal.objects.filter(round_id=round_id).order_by()
        return (
            dict(proposals.values_list("id", "panel_id")),
            rankings.conflicted_user_ids_by_proposal(proposals),
        )

    panel_of, conflicts = _memo(
        cache, ("round_conflicts", round_id), proposals_of_round
    )
    excluded = {
        pk for pk, panel_id in panel_of.items() if panel is None or panel_id == panel.pk
    }
    if discussion_id is not None:
        excluded |= _memo(
            cache,
            ("covered", discussion_id),
            lambda: set(
                _covered_ids(discussion_id).values_list("proposal_id", flat=True)
            ),
        )
    ids: set[int] = set()
    for pk in excluded:
        if pk not in panel_of:
            # Covered from another round: not one loaded above.
            ids |= rankings.conflicted_user_ids(models.Proposal.objects.filter(pk=pk))
        else:
            ids |= conflicts.get(pk, set())
    return ids


def _round_people(call_round, call_step, panel, discussion_id=None, cache=None):
    """Who takes part in a round discussion: the round reports' authors and
    the step's completers for the proposals it covers, and the call's
    managers and the panel's members when the step includes them.

    A round room discusses all of the panel's proposals at once, so nobody
    sits in it who is on the applicant team of, wrote, or has a conflict of
    interest on record with any proposal of the round on that panel (of the
    round, on a call without panels), whatever that proposal's state or
    step: the rule that keeps them from the round's ranking and notes
    (rankings.readable_rounds). A saved discussion also keeps out those of
    every proposal it covered before, moved to another panel since
    (``covered_proposals``).

    The authors and completers of all the covered proposals are worked out
    at once, in a fixed number of queries however many proposals the round
    has (``_round_author_ids``, ``_round_completer_ids``); only a step of
    the call's reviewers asks per proposal, as a reviewer's standing is.
    The per-proposal checks of the resolvers they stand for -- the
    proposal's team, a conflict with it -- are covered by the round-wide
    exclusion below.
    """
    call = call_round.call
    instances = _round_instances(call_round.pk, call_step.pk, panel)
    covered = _round_covered(call_round, call_step, panel, cache)
    completers = _round_completer_ids(call, call_step, instances, covered)
    ids = _round_author_ids(call, call_step, instances, covered) | completers
    if call_step.discussion_call_managers:
        ids |= _call_manager_ids(call, cache)
    if call_step.discussion_panel_members:
        ids |= _panel_member_ids(call, panel, cache)
    ids -= _round_conflicted_ids(call_round.pk, panel, discussion_id, cache)
    moderators = (completers | _chair_ids(call, panel, cache=cache)) & ids
    return ids, moderators


def _call_role_holder_ids(call, role_name) -> set[int]:
    return set(get_users(call, role_name=role_name).values_list("id", flat=True))


def _round_author_ids(call, call_step, instances, covered) -> set[int]:
    """``round_reports.report_authors`` of the covered proposals at once."""
    if not covered:
        return set()
    try:
        source, parameter = parse_contributor_source(call_step.contributor_source)
    except ValueError:
        return set()
    if source == ContributorSources.PROPOSAL_ROLE:
        return proposal_roles.role_holder_ids(
            call,
            covered,
            call_step.contributor_source,
            lead_only=call_step.contributors_lead_only,
        )
    if source != ContributorSources.CALL_ROLE:
        return set()
    if parameter == ResponsibleRoles.PANEL_CHAIR:
        return proposal_permissions.panel_chair_ids(call)
    role_name = proposal_permissions.RESPONSIBLE_ROLE_TO_CALL_ROLE.get(parameter or "")
    if role_name == RoleEnum.CALL_REVIEWER:
        return {
            user.pk
            for instance in instances
            for user in round_reports.report_authors(instance)
        }
    return _call_role_holder_ids(call, role_name) if role_name else set()


def _round_completer_ids(call, call_step, instances, covered) -> set[int]:
    """``notification_rules.step_completer_users`` of the covered proposals
    at once."""
    role = call_step.completer
    if not role or not covered:
        return set()
    if role == ResponsibleRoles.PROPOSAL_ROLE:
        if call_step.completer_proposal_role_id is None:
            return set()
        return proposal_roles.role_holder_ids(
            call,
            covered,
            call_step.completer_proposal_role_id,
            lead_only=proposal_roles.completer_is_lead_only(call_step),
        )
    if role == ResponsibleRoles.PANEL_CHAIR:
        return proposal_permissions.panel_chair_ids(call)
    if role == ResponsibleRoles.OFFERING_MANAGER:
        offering_ids = models.RequestedResource.objects.filter(
            proposal_id__in=covered,
            requested_offering__state=RequestedOfferingStates.ACCEPTED,
        ).values("requested_offering__offering_id")
        return set(proposal_permissions.offering_manager_ids(offering_ids))
    role_name = proposal_permissions.RESPONSIBLE_ROLE_TO_CALL_ROLE.get(role)
    if role_name == RoleEnum.CALL_REVIEWER:
        return {
            pk
            for instance in instances
            for pk in _completer_ids(call_step, instance.proposal)
        }
    return _call_role_holder_ids(call, role_name) if role_name else set()


def people(discussion, cache=None):
    """``(participant ids, moderator ids)`` of ``discussion`` now, saved or
    not: its participants are those working on its step, moderators its
    completers and the panel's chair. Inactive users included: the member
    sync leaves them out. ``cache``, shared by the rows of one response,
    keeps what its discussions have in common."""
    if discussion.proposal_step_id:
        return _proposal_step_people(discussion.proposal_step, cache)
    return _round_step_people(
        discussion.round_step, discussion.panel, discussion.pk, cache
    )


def participant_ids(discussion, cache=None) -> set[int]:
    return people(discussion, cache)[0]


# =============================================================================
# The room scope, as registered with matrix_chat
# =============================================================================


def room_members(discussion):
    """Who belongs in the discussion's room: its participants while it is
    open. Once it closed, a room still active -- closed while Matrix chat
    was disabled, or whose closing export failed -- waits for its history
    to be exported (``ended_ids``), and a sync meanwhile must not take out
    who took part, nor bring anyone in: those who took part and are still
    in it, but whoever has since joined the team of, or been found in
    conflict with, a proposal it covered."""
    if discussion.closed_at is not None:
        in_room = matrix_models.MatrixRoomMember.objects.filter(
            room__content_type=_content_type(),
            room__object_id=discussion.pk,
            membership_state__in=(
                matrix_models.MembershipStates.INVITED,
                matrix_models.MembershipStates.JOINED,
            ),
        ).values("user_id")
        return User.all_objects.filter(
            id__in=discussion.participants.filter(id__in=in_room).values("id")
        ).exclude(id__in=rankings.conflicted_user_ids(covered_proposals(discussion)))
    return User.all_objects.filter(id__in=participant_ids(discussion))


def discussion_ids_for_user(user):
    """The open discussions ``user`` takes part in now, for matrix_chat to
    bring a reactivated user back into the rooms a sync left them out of
    while they were inactive. Only discussions they could take part in are
    asked about: of calls they hold a role on, of proposals they hold an
    evaluator role on or a contribution to, or requesting an offering whose
    team they are on."""
    call_ids = permissions_models.UserRole.objects.filter(
        user=user,
        is_active=True,
        content_type=ContentType.objects.get_for_model(models.Call),
    ).values("object_id")
    held = models.ProposalRoleAssignment.objects.filter(
        user=user, revoked_at__isnull=True
    ).values("proposal_id")
    contributed = models.StepContribution.objects.filter(
        Q(contributor=user) | Q(delegate=user)
    ).values("proposal_step_id")
    offered = models.RequestedResource.objects.filter(
        requested_offering__offering_id__in=permissions_models.UserRole.objects.filter(
            user=user,
            is_active=True,
            content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
        ).values("object_id")
    ).values("proposal_id")
    candidates = _open_discussions().filter(
        Q(proposal_step__proposal__round__call_id__in=call_ids)
        | Q(round_step__round__call_id__in=call_ids)
        | Q(proposal_step__proposal_id__in=held)
        | Q(round_step__round__proposal__id__in=held)
        | Q(proposal_step_id__in=contributed)
        | Q(proposal_step__proposal_id__in=offered)
        | Q(round_step__round__proposal__id__in=offered)
    )
    return [
        discussion.pk
        for discussion in candidates.distinct().select_related(
            "proposal_step__call_step",
            "proposal_step__proposal__round__call",
            "round_step__call_step",
            "round_step__round__call",
            "panel",
        )
        if user.pk in participant_ids(discussion)
    ]


def ended_ids():
    """The closed discussions, whose rooms are archived once Matrix chat can
    export them."""
    return models.StepDiscussion.objects.filter(closed_at__isnull=False).values("id")


def room_power_level(user, discussion) -> int:
    # A sync asks for each member in turn about the scope it loaded once, so
    # the moderators are worked out once per sync.
    moderators = discussion.__dict__.get("_moderator_ids")
    if moderators is None:
        moderators = people(discussion)[1]
        discussion.__dict__["_moderator_ids"] = moderators
    return MODERATOR if user.pk in moderators else 0


# What each kind of participant is called in the room, most specific first:
# someone brought in more than one way is called by the first that holds.
# How a member who joins a discussion already under way is introduced.
ROLE_NOTICE = "{name} joins the discussion as {role}."

PANEL_CHAIR_LABEL = "Panel chair"
CALL_MANAGER_LABEL = "Call manager"
PANEL_MEMBER_LABEL = "Panel member"
REVIEWER_LABEL = "Reviewer"
OFFERING_TEAM_LABEL = "Offering team"
_RESPONSIBLE_ROLE_LABELS = {
    ResponsibleRoles.CALL_MANAGER: CALL_MANAGER_LABEL,
    ResponsibleRoles.OFFERING_MANAGER: OFFERING_TEAM_LABEL,
    ResponsibleRoles.REVIEWER: REVIEWER_LABEL,
    ResponsibleRoles.PANEL_MEMBER: PANEL_MEMBER_LABEL,
    ResponsibleRoles.PANEL_CHAIR: PANEL_CHAIR_LABEL,
}


def _evaluator_role_name(call, role_ref) -> str | None:
    role = proposal_roles.resolve_role(call, role_ref)
    return role.name if role is not None else None


def _completer_label(call_step, call) -> str | None:
    role = call_step.completer
    if role == ResponsibleRoles.PROPOSAL_ROLE:
        return _evaluator_role_name(call, call_step.completer_proposal_role_id)
    return _RESPONSIBLE_ROLE_LABELS.get(role)


def _contributor_label(call_step, call) -> str | None:
    try:
        source, parameter = parse_contributor_source(call_step.contributor_source)
    except ValueError:
        return None
    if source == ContributorSources.PROPOSAL_ROLE:
        return _evaluator_role_name(call, call_step.contributor_source)
    if source == ContributorSources.CALL_ROLE:
        return _RESPONSIBLE_ROLE_LABELS.get(parameter or "")
    if source == ContributorSources.ASSIGNED_REVIEWERS:
        return REVIEWER_LABEL
    if source == ContributorSources.REQUESTED_OFFERING_TEAM:
        return OFFERING_TEAM_LABEL
    return None


def member_roles(discussion) -> dict[int, str]:
    """What each participant of ``discussion`` takes part as, by user id:
    the panel's chair, a call manager, the step's completers and
    contributors by the role that makes them so -- an evaluator role by its
    name -- and the panel's members. Built from what ``people`` adds up,
    for its participants alone."""
    call_step = discussion.call_step
    if discussion.proposal_step_id:
        instance = discussion.proposal_step
        proposal = instance.proposal
        call = proposal.round.call
        panel = panels.panel_of(proposal)
        completers = _completer_ids(call_step, proposal)
        contributors = _contributor_ids(instance)
        chairs = _chair_ids(call, panel, proposal)
    else:
        call_round = discussion.round_step.round
        call = call_round.call
        panel = discussion.panel
        instances = _round_instances(call_round.pk, call_step.pk, panel)
        covered = _round_covered(call_round, call_step, panel)
        completers = _round_completer_ids(call, call_step, instances, covered)
        contributors = _round_author_ids(call, call_step, instances, covered)
        chairs = _chair_ids(call, panel)
    managers = _call_manager_ids(call) if call_step.discussion_call_managers else set()
    members = (
        _panel_member_ids(call, panel) if call_step.discussion_panel_members else set()
    )
    participants, _ = people(discussion)
    completer_label = _completer_label(call_step, call)
    contributor_label = _contributor_label(call_step, call)
    ranked = (
        (chairs, PANEL_CHAIR_LABEL),
        (managers, CALL_MANAGER_LABEL),
        (completers, completer_label),
        (contributors, contributor_label),
        (members, PANEL_MEMBER_LABEL),
    )
    roles: dict[int, str] = {}
    for ids, label in ranked:
        if not label:
            continue
        for pk in ids & participants:
            roles.setdefault(pk, label)
    return roles


def room_role_label(user, discussion) -> str | None:
    # Asked member by member about the scope loaded once, as the power
    # level is: the roles are worked out once per scope object.
    roles = discussion.__dict__.get("_member_roles")
    if roles is None:
        roles = member_roles(discussion)
        discussion.__dict__["_member_roles"] = roles
    return roles.get(user.pk)


def _name_parts(discussion) -> list[str]:
    """The call, the step, and the proposal or the round and panel."""
    call_step = discussion.call_step
    step = call_step.name or call_step.key
    if discussion.proposal_step_id:
        proposal = discussion.proposal_step.proposal
        parts = [proposal.round.call.name, step, proposal.slug or proposal.name]
    else:
        call_round = discussion.round_step.round
        parts = [call_round.call.name, step, call_round.name]
        if discussion.panel_id:
            parts.append(discussion.panel.name)
    return [part for part in parts if part]


def discussion_name(discussion) -> str:
    """ "<call> · <step> · <proposal>", or "<call> · <step> · <round> ·
    <panel>"."""
    return " · ".join(_name_parts(discussion))


def room_subtitle(discussion) -> str:
    """The room's line under its name in a room list: the step, and the
    proposal or the round and panel, without the call."""
    return " · ".join(_name_parts(discussion)[1:])


def room_url(discussion, request=None):
    path = reverse(
        "proposal-step-discussion-detail", kwargs={"uuid": discussion.uuid.hex}
    )
    return request.build_absolute_uri(path) if request is not None else path


def room_welcome(room) -> str:
    return (
        f"**Welcome to the discussion of {room.room_name}.**\n\n"
        "It is for the people working on this step. When the step ends, the "
        "room's history is kept with the step's record and the room closes. "
        "The step's decision is still its completion in Waldur."
    )


def _user_of(who):
    """The user of a request, or the user given."""
    return getattr(who, "user", who)


def managed_call_ids(who):
    """The calls whose discussions the request -- or a bare user -- reads as
    their manager: CALL.UPDATE on the call or its managing organisation
    (permissions.calls_managed_by). A personal access token only reaches
    those its scopes and bindings allow CALL.UPDATE on
    (permissions.configurable_calls)."""
    calls = proposal_permissions.calls_managed_by(_user_of(who))
    if is_pat_auth(getattr(who, "auth", None)):
        calls = proposal_permissions.configurable_calls(who, calls)
    return calls.values("id")


def kept_away(user):
    """A lookup on ``StepDiscussion`` for the discussions ``user`` is kept
    away from, whatever else lets them read: of a proposal they are on the
    applicant team of, wrote, or have a conflict of interest on record with
    (rankings.conflicted_proposals), and the round discussions of a round --
    of a panel, on a call with panels -- with any such proposal."""
    conflicted = rankings.conflicted_proposals(user).order_by()
    return (
        Q(proposal_step__proposal_id__in=conflicted.values("id"))
        # Round discussions that covered one before it changed panel.
        | Q(
            id__in=models.StepDiscussion.covered_proposals.through.objects.filter(
                proposal_id__in=conflicted.values("id")
            ).values("stepdiscussion_id")
        )
        | Q(
            round_step__isnull=False,
            panel__isnull=True,
            round_step__round_id__in=conflicted.values("round_id"),
        )
        | Q(
            Exists(
                conflicted.filter(
                    round_id=OuterRef("round_step__round_id"),
                    panel_id=OuterRef("panel_id"),
                )
            )
        )
    )


def _reads_as_support(who) -> bool:
    """Support, with a personal access token only with its support scope."""
    return _user_of(who).is_support and check_pat_support_scope(who)


def export_reader_ids(who):
    """The discussions whose room history exports the request -- or a bare
    user -- may download: those of the calls it manages (managed_call_ids),
    or for support all of them, except those the user is kept away from
    (kept_away). matrix_chat asks it for support too, so that support
    reads no export of a discussion they apply in."""
    discussions = models.StepDiscussion.objects.all()
    if not _reads_as_support(who):
        calls = managed_call_ids(who)
        discussions = discussions.filter(
            Q(proposal_step__proposal__round__call_id__in=calls)
            | Q(round_step__round__call_id__in=calls)
        )
    return discussions.exclude(kept_away(_user_of(who))).values("id")


def kept_away_ids(user):
    """The ids of the discussions ``user`` is kept away from (kept_away), for
    matrix_chat to keep support out of their rooms."""
    return models.StepDiscussion.objects.filter(kept_away(user)).values("id")


def may_join(user, discussion) -> bool:
    """Whether support may join the discussion's room on their own, or stay
    in it once joined: not one they are kept away from (kept_away)."""
    return not kept_away_ids(user).filter(pk=discussion.pk).exists()


# =============================================================================
# Who reads them
# =============================================================================


def _content_type():
    return ContentType.objects.get_for_model(models.StepDiscussion)


def _in_room_ids(user):
    """The discussions whose room ``user`` -- or a user id -- is in."""
    return matrix_models.MatrixRoomMember.objects.filter(
        user=user,
        membership_state__in=(
            matrix_models.MembershipStates.INVITED,
            matrix_models.MembershipStates.JOINED,
        ),
        room__content_type=_content_type(),
    ).values("room__object_id")


def participating_ids(user):
    """The discussions ``user`` takes part in: open ones whose room they are
    in, and closed ones they took part in. Opening one gives nothing by
    itself: the opener reads it once the room's sync took them in, which it
    does only while they still take part."""
    in_room = _in_room_ids(user)
    took_part = models.StepDiscussion.participants.through.objects.filter(
        user=user
    ).values("stepdiscussion_id")
    return models.StepDiscussion.objects.filter(
        Q(closed_at__isnull=True, id__in=in_room) | Q(id__in=took_part)
    ).values("id")


def _participation(who):
    """A lookup on ``StepDiscussion`` for those the request takes part in
    (participating_ids), or None for none.

    A personal access token is held as the proposal routes hold it: to its
    bindings, through the calls and proposals they reach (PATScopeListFilter)
    -- a discussion of a bound call, or of a bound proposal's step -- and to
    its scopes: taking part is reading the proposals evaluated, so it needs
    PROPOSAL.LIST. Without it, it reads none through taking part."""
    taking_part = Q(id__in=participating_ids(_user_of(who)))
    auth = getattr(who, "auth", None)
    if not is_pat_auth(auth):
        return taking_part
    reach = _token_reach(who)
    if reach is None:
        return None
    calls, proposals = reach
    return taking_part & (
        Q(proposal_step__proposal__round__call_id__in=calls)
        | Q(round_step__round__call_id__in=calls)
        | Q(proposal_step__proposal_id__in=proposals)
    )


def _token_reach(who):
    """``(call ids, proposal ids)`` a personal access token reaches taking
    part, as the proposal routes hold it: within its bindings, and only with
    PROPOSAL.LIST; None without it."""
    auth = getattr(who, "auth", None)
    if PermissionEnum.LIST_PROPOSALS.value not in getattr(auth, "scopes", ()):
        return None

    def bound(queryset):
        # Applied outside a view, as matrix_chat's export filter does.
        return (
            PATScopeListFilter()
            .filter_queryset(who, queryset, None)  # pyright: ignore[reportArgumentType]
            .values("id")
        )

    return bound(models.Call.objects.all()), bound(models.Proposal.objects.all())


TOKEN_CANNOT_OPEN = (
    "This personal access token does not reach the step's discussion: it "
    "needs PROPOSAL.LIST within its bindings to the call or proposal, or "
    "CALL.UPDATE on the call."
)


def check_token(who, proposal_step=None, round_step=None):
    """Raise unless the request's personal access token could read the
    discussion it opens: as taking part, PROPOSAL.LIST within its bindings
    to the call or the proposal; or as the call's manager, CALL.UPDATE where
    its scopes and bindings allow it (``SHOWS``). Any other request passes."""
    if not is_pat_auth(getattr(who, "auth", None)):
        return
    if proposal_step is not None:
        proposal_id = proposal_step.proposal_id
        call_id = proposal_step.proposal.round.call_id
    else:
        proposal_id = None
        call_id = round_step.round.call_id
    if managed_call_ids(who).filter(id=call_id).exists():
        return
    reach = _token_reach(who)
    if reach is not None:
        calls, proposals = reach
        if calls.filter(id=call_id).exists() or (
            proposal_id is not None and proposals.filter(id=proposal_id).exists()
        ):
            return
    raise exceptions.PermissionDenied(TOKEN_CANNOT_OPEN)


def visible_to(who):
    """The discussions the request -- or a bare user -- reads (``SHOWS``)."""
    user = _user_of(who)
    discussions = models.StepDiscussion.objects.all()
    if not user.is_authenticated:
        return discussions.none()
    if user.is_staff:
        return discussions
    if not _reads_as_support(who):
        readable = Q(id__in=export_reader_ids(who))
        if (taking_part := _participation(who)) is not None:
            readable |= taking_part
        discussions = discussions.filter(readable)
    return discussions.exclude(kept_away(user))


def room_of(discussion):
    return matrix_models.MatrixRoom.objects.filter(
        content_type=_content_type(), object_id=discussion.pk
    ).first()


def closing_export(room):
    """The history export of the room's closing: while it is being closed,
    the room's own; once archived, its latest export on closing."""
    if room is None:
        return None
    if room.closing_export_id:
        return room.closing_export
    return (
        room.exports.filter(export_type=matrix_models.ExportTypes.ON_DELETION)
        .order_by("-created")
        .first()
    )


# =============================================================================
# Opening
# =============================================================================


def target_of(proposal_step, cache=None):
    """``(proposal_step, round_step, panel)`` of the discussion a proposal's
    step belongs to: its own, or on a step run once per round the round's
    discussion of the proposal's panel. Raises when there is none yet."""
    call_step = proposal_step.call_step
    if not call_step.round_scoped:
        return proposal_step, None, None
    proposal = proposal_step.proposal
    round_step = round_reports.round_step_of(proposal.round, call_step)
    if round_step is None or not round_step.is_open:
        raise DiscussionConflict(
            "The step has not been opened for the round yet, so neither has "
            "its discussion."
        )
    if not _has_panels(proposal.round.call, cache):
        return None, round_step, None
    if proposal.panel_id is None:
        raise DiscussionConflict(
            "The proposal has no panel yet, and a round step's discussion is "
            "held per panel."
        )
    return None, round_step, proposal.panel


def round_target(round_step, panel=None, cache=None):
    """``(None, round_step, panel)``, the panel checked against the call: one
    of its panels on a call with panels, none on a call without."""
    call = round_step.round.call
    if _has_panels(call, cache):
        if panel is None:
            raise exceptions.ValidationError(
                {
                    "panel_uuid": "The call has panels: name the panel whose "
                    "discussion to open."
                }
            )
        if panel.call_id != call.pk:
            raise exceptions.ValidationError(
                {"panel_uuid": "The call has no such panel."}
            )
        return None, round_step, panel
    if panel is not None:
        raise exceptions.ValidationError({"panel_uuid": "The call has no such panel."})
    return None, round_step, None


def find(proposal_step=None, round_step=None, panel=None):
    """The step's latest discussion: the open one, else the last closed.
    A step reactivated after its discussion closed opens a new one, and
    the earlier ones stay on record."""
    if proposal_step is not None:
        discussions = models.StepDiscussion.objects.filter(proposal_step=proposal_step)
    else:
        discussions = models.StepDiscussion.objects.filter(
            round_step=round_step, panel=panel
        )
    return discussions.order_by(
        F("closed_at").desc(nulls_first=True), "-created", "-id"
    ).first()


def is_running(proposal_step=None, round_step=None, panel=None) -> bool:
    """Whether the step is being worked on: the proposal's step active, or
    the round step opened with a proposal of the panel at it."""
    if proposal_step is not None:
        # A proposal rejected or withdrawn leaves its steps where they stood.
        return (
            proposal_step.status == WorkflowStepInstanceStatuses.ACTIVE
            and proposal_step.proposal.state not in ENDED_PROPOSAL_STATES
        )
    if round_step is None or not round_step.is_open:
        return False
    return (
        round_instances(round_step, panel)
        .filter(
            status=WorkflowStepInstanceStatuses.ACTIVE,
            proposal__state=ProposalStates.IN_REVIEW,
        )
        .exists()
    )


def _call_step_of(proposal_step, round_step):
    step = proposal_step if proposal_step is not None else round_step
    if step is None:
        raise ValueError("Name a proposal's step or a round step.")
    return step.call_step


def _step_participant_ids(proposal_step) -> set[int]:
    """Who takes part in the discussion a proposal's step belongs to, also
    while it cannot be opened yet: a step run once per round before the
    round step was opened, or before the proposal has its panel."""
    call_step = proposal_step.call_step
    if not call_step.round_scoped:
        return participant_ids(models.StepDiscussion(proposal_step=proposal_step))
    proposal = proposal_step.proposal
    return _round_people(proposal.round, call_step, panels.panel_of(proposal))[0]


def _takes_part_in_round(user, round_step, panel) -> bool:
    """Whether ``user`` takes part in the round step's discussion of
    ``panel``. A panel of another call is no discussion anyone takes part
    in; with none named on a call with panels, taking part in any of its
    panels' discussions lets round_target say a panel must be named."""
    call = round_step.round.call
    if panel is not None:
        if panel.call_id != call.pk:
            return False
        candidates = [panel]
    else:
        candidates = list(call.panels.all()) or [None]
    for candidate in candidates:
        discussion = find(None, round_step, candidate)
        if discussion is None or discussion.closed_at is not None:
            discussion = models.StepDiscussion(round_step=round_step, panel=candidate)
        if user.pk in participant_ids(discussion):
            return True
    return False


def check_open(user, proposal_step=None, round_step=None, panel=None, discussion=None):
    """Raise unless ``user`` may open the discussion now (``OPENS``).

    Whoever does not take part is refused first, the same way whatever the
    step's state, so that the answer tells them nothing about it."""
    call_step = _call_step_of(proposal_step, round_step)
    # The open one keeps out what it covered before; a new one starts anew.
    candidate = (
        discussion
        if discussion is not None and discussion.closed_at is None
        else models.StepDiscussion(
            proposal_step=proposal_step, round_step=round_step, panel=panel
        )
    )
    if user.pk not in participant_ids(candidate):
        raise exceptions.PermissionDenied(NOT_TAKING_PART)
    # A record left from before the discussion was switched off opens
    # nothing either.
    if not call_step.has_discussion:
        raise exceptions.ValidationError(
            "This step has no discussion. A call manager can give it one in "
            "the call's workflow."
        )
    if not matrix_client.is_enabled():
        raise exceptions.ValidationError(MATRIX_DISABLED)
    if is_running(proposal_step, round_step, panel):
        # A closed discussion of a step running again is followed by a
        # new one.
        return
    if discussion is not None and discussion.closed_at is not None:
        raise DiscussionConflict(
            "The discussion closed when the step ended; its history is kept "
            "with the step's record."
        )
    raise DiscussionConflict("The step is not active, so its discussion cannot open.")


def _lock_running(round_step, panel):
    """Lock the proposals' steps that keep the round discussion running
    (is_running), after the round step, as setting its due date locks them.
    The last of them completing or withdrawn meanwhile is read as such, and
    one completing after this waits for the discussion opened here, which
    its closing then finds."""
    list(
        round_instances(round_step, panel)
        .filter(
            status=WorkflowStepInstanceStatuses.ACTIVE,
            proposal__state=ProposalStates.IN_REVIEW,
        )
        .select_for_update(of=("self",))
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def open_discussion(user, proposal_step=None, round_step=None, panel=None):
    """Open the discussion of a proposal's step, or of a round step for a
    panel, as ``user``: create it and its room the first time, return the
    same one afterwards. Returns ``(discussion, created)``."""
    if proposal_step is not None:
        # Before target_of, whose refusals say where the round step stands.
        if user.pk not in _step_participant_ids(proposal_step):
            raise exceptions.PermissionDenied(NOT_TAKING_PART)
        proposal_step, round_step, panel = target_of(proposal_step)
    else:
        # Before round_target, whose refusals say which panels the call has.
        if not _takes_part_in_round(user, round_step, panel):
            raise exceptions.PermissionDenied(NOT_TAKING_PART)
        proposal_step, round_step, panel = round_target(round_step, panel)
    with transaction.atomic():
        # Two people opening it at once get the one discussion.
        if proposal_step is not None:
            # The proposal first, in the order a withdrawal or a completion
            # locks them: one withdrawn meanwhile is read as withdrawn, and
            # its closing cannot miss a discussion opened behind it.
            models.Proposal.objects.select_for_update(of=("self",)).get(
                pk=proposal_step.proposal_id
            )
            proposal_step = (
                models.ProposalWorkflowStepInstance.objects.select_for_update(
                    of=("self",)
                )
                .select_related("call_step", "proposal__round__call")
                .get(pk=proposal_step.pk)
            )
        elif round_step is not None:
            round_step = (
                models.RoundStep.objects.select_for_update(of=("self",))
                .select_related("call_step", "round__call")
                .get(pk=round_step.pk)
            )
            _lock_running(round_step, panel)
        discussion = find(proposal_step, round_step, panel)
        check_open(user, proposal_step, round_step, panel, discussion)
        # After the checks on who takes part: a stranger learns nothing of
        # the call. An archived call's evaluation gets no discussion; its
        # award stage does, as the stale sweep treats it as live.
        if proposal_step is not None:
            archive.refuse_step(proposal_step.proposal, proposal_step)
        else:
            archive.refuse(round_step)
        # The first, or a new one once the last closed and the step runs
        # again: the closed one keeps its record, room and export.
        created = False
        if discussion is None or discussion.closed_at is not None:
            discussion = models.StepDiscussion.objects.create(
                proposal_step=proposal_step,
                round_step=round_step,
                panel=panel,
                opened_by=user,
            )
            cover(discussion)
            created = True
        try:
            _, room_created = room_provisioning.ensure_room(
                discussion, created_by=user, name=discussion_name(discussion)
            )
        except room_provisioning.MatrixChatDisabled:
            # Switched off since check_open asked; leaving the atomic block
            # with the error keeps no record.
            raise exceptions.ValidationError(MATRIX_DISABLED)
        if not room_created:
            # Brings in whoever came to the step since the last sync.
            matrix_tasks.resync_scope_rooms(discussion)
    if created:
        logger.info(
            "User %s opened discussion %s (%s)",
            user.uuid.hex,
            discussion.uuid.hex,
            discussion_name(discussion),
        )
    return discussion, created


# =============================================================================
# Closing
# =============================================================================


def close(discussion) -> bool:
    """Close ``discussion``: keep who took part -- its participants now, and
    whoever is still in its room -- and close its room. False when it was
    closed already."""
    with transaction.atomic():
        discussion = models.StepDiscussion.objects.select_for_update().get(
            pk=discussion.pk
        )
        if discussion.closed_at is not None:
            return False
        ids = participant_ids(discussion)
        room = room_of(discussion)
        if room is not None:
            # Open contributions are withdrawn as the step ends, before this
            # runs: whoever was in the room took part all the same -- but
            # not someone kept away from it since, support who joined with
            # Join among them, whom no sync has taken out yet.
            in_room = set(
                matrix_models.MatrixRoomMember.objects.filter(
                    room=room,
                    membership_state__in=(
                        matrix_models.MembershipStates.INVITED,
                        matrix_models.MembershipStates.JOINED,
                    ),
                ).values_list("user_id", flat=True)
            )
            if in_room - ids:
                in_room -= rankings.conflicted_user_ids(covered_proposals(discussion))
            ids |= in_room
        discussion.closed_at = timezone.now()
        discussion.save(update_fields=["closed_at", "modified"])
        discussion.participants.set(ids)
        if room is not None:
            # While Matrix chat is disabled the room stays as it is, its
            # members kept (room_members); the nightly export closes it once
            # chat is back (matrix_chat.tasks.close_rooms_of_ended_scopes).
            matrix_tasks.close_room_on_commit(
                room, reason=ROOM_CLOSING_REASON, export_history=True
            )
    logger.info("Discussion %s closed with its step", discussion.uuid.hex)
    return True


def _open_discussions():
    return models.StepDiscussion.objects.filter(closed_at__isnull=True)


def _live_rooms_discussions():
    """The discussions whose rooms a change of who belongs in them must
    reach: the open ones, and the closed ones whose rooms are still active
    -- closed while Matrix chat was disabled, or whose closing export
    failed -- so that whoever has since been kept away leaves those too
    (``room_members``)."""
    active_rooms = matrix_models.MatrixRoom.objects.filter(
        content_type=_content_type(),
        state=matrix_models.RoomStates.ACTIVE,
    ).values("object_id")
    return models.StepDiscussion.objects.filter(
        Q(closed_at__isnull=True) | Q(id__in=active_rooms)
    )


def _round_still_running(round_step, panel) -> bool:
    """Whether a proposal of the round under evaluation is at the round step,
    or still to reach it, on the panel."""
    instances = models.ProposalWorkflowStepInstance.objects.filter(
        proposal__round_id=round_step.round_id,
        call_step_id=round_step.call_step_id,
        run__isnull=True,
        status__in=(
            WorkflowStepInstanceStatuses.PENDING,
            WorkflowStepInstanceStatuses.ACTIVE,
        ),
        proposal__state__in=UNDER_EVALUATION,
    )
    if panel is not None:
        instances = instances.filter(proposal__panel=panel)
    return instances.exists()


def close_finished_round(round_id, call_step_id):
    """Close the round discussions of the round step whose proposals are all
    done with it.

    Each discussion is locked before its proposals are counted, in a fixed
    order: of the last two proposals of a panel leaving the step at once,
    the second to get the lock sees the first gone, so one of them closes
    it. The nightly close_stale is the backstop for whatever else is
    missed."""
    with transaction.atomic():
        discussions = list(
            _open_discussions()
            .filter(
                round_step__round_id=round_id, round_step__call_step_id=call_step_id
            )
            .select_related("round_step", "panel")
            .select_for_update(of=("self",))
            .order_by("pk")
        )
        for discussion in discussions:
            if not _round_still_running(discussion.round_step, discussion.panel):
                close(discussion)


def close_ended(instance):
    """Close the discussions a proposal's step ended for, once it left the
    active status: its own, and the round's when it was the last of its
    panel at the round step."""
    if dry_run_context.current() is not None:
        return
    if instance.status == WorkflowStepInstanceStatuses.ACTIVE:
        return
    for discussion in _open_discussions().filter(proposal_step=instance):
        close(discussion)
    if instance.run_id is None and instance.call_step.round_scoped:
        close_finished_round(instance.proposal.round_id, instance.call_step_id)


def close_step_discussions(call_step):
    """Close the open discussions of a call step whose discussion was
    switched off, as when the step ends: who took part is kept, and the
    rooms' history exported."""
    if dry_run_context.current() is not None:
        return
    for discussion in _open_discussions().filter(
        Q(proposal_step__call_step=call_step) | Q(round_step__call_step=call_step)
    ):
        close(discussion)


def _still_running_in_round():
    """An expression on ``StepDiscussion``: whether a proposal under
    evaluation is at its round step, or still to reach it, on its panel
    (``_round_still_running``)."""
    instances = models.ProposalWorkflowStepInstance.objects.filter(
        proposal__round_id=OuterRef("round_step__round_id"),
        call_step_id=OuterRef("round_step__call_step_id"),
        run__isnull=True,
        status__in=(
            WorkflowStepInstanceStatuses.PENDING,
            WorkflowStepInstanceStatuses.ACTIVE,
        ),
        proposal__state__in=UNDER_EVALUATION,
    )
    return Q(Q(panel__isnull=True), Exists(instances)) | Q(
        Q(panel__isnull=False),
        Exists(instances.filter(proposal__panel_id=OuterRef("panel_id"))),
    )


def stale_ids():
    """The open discussions whose step no longer runs: a proposal's step no
    longer active, or its proposal rejected or withdrawn; a round step none
    of whose proposals of the panel under evaluation is at it or still to
    reach it; a step whose discussion was switched off. Their closing
    follows those events, so these are the ones an event was missed for."""
    return (
        _open_discussions()
        .filter(
            Q(
                Q(proposal_step__isnull=False),
                ~Q(proposal_step__status=WorkflowStepInstanceStatuses.ACTIVE)
                | Q(proposal_step__proposal__state__in=ENDED_PROPOSAL_STATES)
                | Q(proposal_step__call_step__has_discussion=False),
            )
            | Q(
                Q(round_step__isnull=False),
                ~_still_running_in_round()
                | Q(round_step__call_step__has_discussion=False),
            )
        )
        # An archived call's evaluation discussions stay as they are; its
        # award stage goes on.
        .exclude(
            Q(proposal_step__proposal__round__call__state=CallStates.ARCHIVED)
            & ~archive.post_award_step_q("proposal_step__call_step")
        )
        .exclude(round_step__round__call__state=CallStates.ARCHIVED)
        .order_by("pk")
        .values_list("id", flat=True)
    )


def close_stale() -> int:
    """Close the open discussions whose step no longer runs (``stale_ids``),
    as their step's end would have. Run nightly: a discussion is closed by
    the events that end its step, and this catches one they missed, such
    as the last two proposals of a panel leaving its round step at once.
    Returns how many were closed."""
    closed = 0
    for pk in list(stale_ids()):
        if close(models.StepDiscussion(pk=pk)):
            closed += 1
    return closed


def close_proposal(proposal):
    """Close the discussions of a proposal rejected or withdrawn, whose
    steps stop wherever they stood; and the round's ones it was the last
    proposal of."""
    if dry_run_context.current() is not None:
        return
    for discussion in _open_discussions().filter(proposal_step__proposal=proposal):
        close(discussion)
    steps = (
        _open_discussions()
        .filter(round_step__round_id=proposal.round_id)
        .values_list("round_step__call_step_id", flat=True)
        .distinct()
    )
    for call_step_id in list(steps):
        close_finished_round(proposal.round_id, call_step_id)


# =============================================================================
# Keeping the rooms in line
# =============================================================================


# Where the ids of the discussions to resync gather until the transaction
# commits, on the thread's database connection.
_PENDING_RESYNC = "_step_discussions_to_resync"


def _resync(discussions):
    """Sync the rooms of ``discussions`` once the transaction commits, each
    once however many changes in it asked for it."""
    if dry_run_context.current() is not None or not matrix_client.is_enabled():
        return
    ids = set(discussions.values_list("id", flat=True))
    if not ids:
        return
    connection = transaction.get_connection()
    pending = getattr(connection, _PENDING_RESYNC, None)
    if pending is None:
        pending = set()
        setattr(connection, _PENDING_RESYNC, pending)
    pending |= ids
    # Registered by every change, so that one rolled back leaves none
    # waiting: the first to run takes them all, the others find none.
    transaction.on_commit(_dispatch_resync)


def _dispatch_resync():
    connection = transaction.get_connection()
    ids = getattr(connection, _PENDING_RESYNC, None)
    setattr(connection, _PENDING_RESYNC, None)
    if not ids:
        return
    for discussion in _live_rooms_discussions().filter(id__in=ids):
        matrix_tasks.resync_scope_rooms(discussion)


def _of_call(call_id):
    return Q(proposal_step__proposal__round__call_id=call_id) | Q(
        round_step__round__call_id=call_id
    )


def _of_steps(step_ids):
    return Q(proposal_step__call_step_id__in=step_ids) | Q(
        round_step__call_step_id__in=step_ids
    )


def _of_panel(panel):
    """A panel's proposals' own discussions, and its round discussions."""
    return Q(proposal_step__proposal__panel=panel) | Q(
        round_step__isnull=False, panel=panel
    )


def _without_panel():
    """Discussions without a panel, whose chair is the call's."""
    return Q(proposal_step__proposal__panel__isnull=True) | Q(
        round_step__isnull=False, panel__isnull=True
    )


def _steps(lookup, call_id=None):
    steps = models.CallWorkflowStep.objects.filter(lookup)
    if call_id is not None:
        steps = steps.filter(call_id=call_id)
    return steps.values("id")


def _call_role_source(role):
    return f"{ContributorSources.CALL_ROLE}:{role}"


def _steps_of_call_role(role_name):
    """A lookup on call steps whose discussions take people from the call
    role ``role_name``: as observers, contributors or completers. None for
    a role no discussion takes anyone from."""
    if role_name == RoleEnum.CALL_MANAGER:
        return (
            Q(discussion_call_managers=True)
            | Q(completer=ResponsibleRoles.CALL_MANAGER)
            | Q(contributor_source=_call_role_source(ResponsibleRoles.CALL_MANAGER))
        )
    if role_name == RoleEnum.CALL_PANEL_MEMBER:
        return (
            Q(discussion_panel_members=True)
            | Q(
                completer__in=(
                    ResponsibleRoles.PANEL_MEMBER,
                    ResponsibleRoles.PANEL_CHAIR,
                )
            )
            | Q(
                contributor_source__in=(
                    _call_role_source(ResponsibleRoles.PANEL_MEMBER),
                    _call_role_source(ResponsibleRoles.PANEL_CHAIR),
                )
            )
        )
    if role_name == RoleEnum.CALL_REVIEWER:
        return (
            Q(completer=ResponsibleRoles.REVIEWER)
            | Q(contributor_source=_call_role_source(ResponsibleRoles.REVIEWER))
            | Q(contributor_source=ContributorSources.ASSIGNED_REVIEWERS)
        )
    return None


def resync_step(instance_id):
    """A contribution to the proposal's step was given, taken, handed on or
    closed."""
    _resync(_live_rooms_discussions().filter(proposal_step_id=instance_id))


def resync_round_step(round_id, call_step_id):
    """A proposal reached the round step, or left it."""
    _resync(
        _live_rooms_discussions().filter(
            round_step__round_id=round_id, round_step__call_step_id=call_step_id
        )
    )


def resync_proposal(proposal):
    """Who evaluates the proposal changed: an evaluator role, a conflict of
    interest, its team or its panel. Its own discussions, and its round's."""
    _resync(
        _live_rooms_discussions().filter(
            Q(proposal_step__proposal=proposal)
            | Q(round_step__round_id=proposal.round_id)
        )
    )


def resync_offering_requests(proposals):
    """What ``proposals`` (ids, or a query of them) request changed, or who
    is on the team of an offering they request: their open discussions, and
    their rounds', on steps an offering's team works on -- contributors or
    completers."""
    steps = _steps(
        Q(contributor_source=ContributorSources.REQUESTED_OFFERING_TEAM)
        | Q(per_requested_offering=True)
        | Q(completer=ResponsibleRoles.OFFERING_MANAGER)
    )
    _resync(
        _live_rooms_discussions()
        .filter(
            _of_steps(steps)
            & (
                Q(proposal_step__proposal_id__in=proposals)
                | Q(round_step__round__proposal__id__in=proposals)
            )
        )
        .distinct()
    )


def resync_offering(offering_id):
    """A role on an offering was granted or revoked: the discussions of the
    proposals requesting it."""
    resync_offering_requests(
        models.RequestedResource.objects.filter(
            requested_offering__offering_id=offering_id
        ).values("proposal_id")
    )


def resync_call_role(call_id, role_name, user_id):
    """A role on the call was granted to, or revoked from, the user: the
    discussions of the call's steps that take people from it, and those
    whose room the user is in, where who moderates may change with it."""
    lookup = Q(id__in=_in_room_ids(user_id))
    steps = _steps_of_call_role(role_name)
    if steps is not None:
        lookup |= _of_steps(_steps(steps, call_id))
    _resync(_live_rooms_discussions().filter(_of_call(call_id) & lookup))


def resync_panel_members(panel):
    """Members joined or left the panel: its discussions on steps that take
    in the panel's members. Evaluator roles held through it end on their
    own (resync_proposal)."""
    steps = _steps(Q(discussion_panel_members=True), panel.call_id)
    _resync(_live_rooms_discussions().filter(_of_panel(panel) & _of_steps(steps)))


def resync_panel_chair(panel):
    """The panel's chair changed: its discussions, which the chair
    moderates."""
    _resync(_live_rooms_discussions().filter(_of_panel(panel)))


def resync_panel_added(call_id):
    """A panel was added to the call: a call with panels takes no panel
    members into a discussion without a panel."""
    steps = _steps(Q(discussion_panel_members=True), call_id)
    _resync(_live_rooms_discussions().filter(_of_steps(steps) & _without_panel()))


def resync_call_chairs(call_id):
    """The call's panel chair or vice-chair changed: the discussions they
    moderate, those without a panel, and those of steps they contribute to
    or complete."""
    steps = _steps(
        Q(completer=ResponsibleRoles.PANEL_CHAIR)
        | Q(contributor_source=_call_role_source(ResponsibleRoles.PANEL_CHAIR)),
        call_id,
    )
    _resync(
        _live_rooms_discussions().filter(
            _of_call(call_id) & (_without_panel() | _of_steps(steps))
        )
    )


def resync_call_step(call_step):
    """The step's discussion now includes, or leaves out, the call's
    managers or the panel's members."""
    _resync(_live_rooms_discussions().filter(_of_steps([call_step.pk])))


def resync_call(call_id):
    """A conflict of interest recorded on the call changed: all of its
    discussions."""
    _resync(_live_rooms_discussions().filter(_of_call(call_id)))


# =============================================================================
# What a step shows its viewer
# =============================================================================


def _covered_proposals(
    proposal_step=None, round_step=None, panel=None, discussion=None
):
    """The proposals a discussion is about, for who is kept away from it: a
    proposal's own step its proposal; a round discussion every proposal of
    the round on its panel, and every one ``discussion`` covered before."""
    discussion_id = discussion.pk if discussion is not None else None
    if proposal_step is not None:
        proposal = proposal_step.proposal
        if not proposal_step.call_step.round_scoped:
            return models.Proposal.objects.filter(pk=proposal.pk)
        return _round_exclusion(
            proposal.round_id, panels.panel_of(proposal), discussion_id
        )
    if round_step is None:
        raise ValueError("Name a proposal's step or a round step.")
    return _round_exclusion(round_step.round_id, panel, discussion_id)


# The rows of one response ask the same questions again and again -- whether
# the call has panels, whether the viewer oversees it, which proposals they
# are in conflict with -- so viewer_state keeps the answers in the cache it
# is given, alongside its results.


def _has_panels(call, cache=None) -> bool:
    return _memo(cache, ("has_panels", call.pk), call.panels.exists)


def call_panels(call, cache=None):
    """The call's panels, in order."""
    return _memo(
        cache,
        ("panels", call.pk),
        lambda: list(call.panels.order_by("order", "id")),
    )


def _conflicted_ids(user, cache=None) -> set[int]:
    """The proposals ``user`` is on the applicant team of, wrote, or has a
    conflict of interest on record with (rankings.conflicted_proposals)."""
    return _memo(
        cache,
        ("conflicted", user.pk),
        lambda: set(
            rankings.conflicted_proposals(user).order_by().values_list("id", flat=True)
        ),
    )


def _is_kept_away(
    user, proposal_step=None, round_step=None, panel=None, discussion=None, cache=None
) -> bool:
    """Whether ``kept_away`` holds for ``user`` on the discussion of the
    proposal's step, or of the round step for the panel; never for staff."""
    if user.is_staff:
        return False
    conflicted = _conflicted_ids(user, cache)
    if not conflicted:
        return False
    covered = _covered_proposals(proposal_step, round_step, panel, discussion)
    return covered.filter(pk__in=conflicted).exists()


def _oversees(who, call, cache=None) -> bool:
    user = _user_of(who)
    if user.is_staff or user.is_support:
        return True
    return _memo(
        cache,
        ("oversees", call.pk),
        lambda: managed_call_ids(who).filter(id=call.pk).exists(),
    )


def _latest_by(discussions, field):
    """The latest discussion (``find``) per value of ``field``."""
    latest = {}
    for discussion in discussions.select_related(
        "proposal_step__call_step",
        "proposal_step__proposal__round__call",
        "round_step__call_step",
        "round_step__round__call",
        "panel",
    ).order_by(F("closed_at").desc(nulls_first=True), "-created", "-id"):
        latest.setdefault(getattr(discussion, field), discussion)
    return latest


def _prime_proposals(cache, proposal_ids):
    """Load the latest discussions of the proposals' steps at once."""
    missing = [
        pk for pk in proposal_ids if ("discussions_of_proposal", pk) not in cache
    ]
    if not missing:
        return
    by_proposal = {pk: {} for pk in missing}
    latest = _latest_by(
        models.StepDiscussion.objects.filter(proposal_step__proposal_id__in=missing),
        "proposal_step_id",
    )
    for discussion in latest.values():
        proposal_id = discussion.proposal_step.proposal_id
        by_proposal[proposal_id][discussion.proposal_step_id] = discussion
    for pk, discussions in by_proposal.items():
        cache[("discussions_of_proposal", pk)] = discussions


def _find(proposal_step, round_step, panel, cache=None):
    """``find``, from the discussions of the proposal, or of the round step,
    loaded once per response."""
    if cache is None:
        return find(proposal_step, round_step, panel)
    if proposal_step is not None:
        _prime_proposals(cache, [proposal_step.proposal_id])
        return cache[("discussions_of_proposal", proposal_step.proposal_id)].get(
            proposal_step.pk
        )
    by_panel = _memo(
        cache,
        ("discussions_of_round_step", round_step.pk),
        lambda: _latest_by(
            models.StepDiscussion.objects.filter(round_step=round_step), "panel_id"
        ),
    )
    return by_panel.get(panel.pk if panel is not None else None)


def _is_running(target, cache=None) -> bool:
    """``is_running``; for a round step, which of its panels run is asked
    once per response."""
    proposal_step, round_step, panel = target
    if proposal_step is not None or cache is None:
        return is_running(proposal_step, round_step, panel)
    if not round_step.is_open:
        return False
    running = _memo(
        cache,
        ("running_panels", round_step.pk),
        lambda: set(
            round_instances(round_step, None)
            .filter(
                status=WorkflowStepInstanceStatuses.ACTIVE,
                proposal__state=ProposalStates.IN_REVIEW,
            )
            .order_by()
            .values_list("proposal__panel_id", flat=True)
            .distinct()
        ),
    )
    return bool(running) if panel is None else panel.pk in running


def _takes_part(user, candidate, cache=None) -> bool:
    """Whether ``user`` takes part in ``candidate`` -- a discussion, saved or
    not -- once kept_away has been ruled out for them.

    A call manager or a panel member a step takes in for that alone is
    found from the call's people, asked once per response; anyone else
    from the discussion's participants, worked out once per discussion."""
    call_step = candidate.call_step
    call = candidate.call
    if call_step.discussion_call_managers and user.pk in _call_manager_ids(call, cache):
        return True
    key = (
        "participants",
        candidate.proposal_step_id,
        candidate.round_step_id,
        candidate.panel_id,
        candidate.pk,
    )
    return user.pk in _memo(cache, key, lambda: participant_ids(candidate, cache))


def startable_for(user, request=None):
    """The discussions ``user`` could start now that nobody has started: of
    the active steps they work on, and of the opened round steps they take
    part in, panel by panel. Each is ``(proposal_step, round_step, panel)``,
    the target ``open_discussion`` takes. A discussion closed while its step
    runs again is listed too, as opening it starts a new one.

    The candidates are the steps within the user's reach, as their task list
    finds them (``my_tasks.step_candidates``); ``viewer_state`` then decides
    each one by the rules that let them open it, with what the candidates
    share asked once."""
    if user is None or not user.is_authenticated or not matrix_client.is_enabled():
        return []
    standing = my_tasks.Standing(user)
    cache = {}
    found = []

    # A personal access token lists only what it could open (check_token):
    # what the rest of the response would name, it may not read.
    held_to_token = request is not None and is_pat_auth(getattr(request, "auth", None))

    def startable(**target):
        state = viewer_state(user, cache=cache, request=request, **target)
        if state is None or not state["can_open"] or state["state"] == OPEN:
            return False
        if held_to_token:
            try:
                check_token(request, **target)
            except exceptions.PermissionDenied:
                return False
        return True

    open_discussions = models.StepDiscussion.objects.filter(closed_at__isnull=True)
    instances = list(
        my_tasks.step_candidates(standing, [WorkflowStepInstanceStatuses.ACTIVE])
        .filter(call_step__has_discussion=True, call_step__round_scoped=False)
        .exclude(proposal__state__in=ENDED_PROPOSAL_STATES)
        .exclude(Exists(open_discussions.filter(proposal_step_id=OuterRef("pk"))))
        .order_by("deadline", "id")
    )
    _prime_proposals(cache, {instance.proposal_id for instance in instances})
    for instance in instances:
        if startable(proposal_step=instance):
            found.append((instance, None, None))

    reach = (
        Q(round__call_id__in=list(standing.call_roles))
        | Q(round__call__manager_id__in=standing.organiser_ids)
        | Q(round__call__manager_id__in=standing.updatable_organisation_ids)
        | Q(round__proposal__id__in=standing.held_role_proposal_ids)
    )
    round_steps = (
        models.RoundStep.objects.filter(
            reach,
            opened_at__isnull=False,
            call_step__has_discussion=True,
            call_step__round_scoped=True,
        )
        .select_related("round__call", "call_step")
        .distinct()
        .order_by("due_at", "id")
    )
    for round_step in round_steps:
        for panel in call_panels(round_step.round.call, cache) or [None]:
            if startable(round_step=round_step, panel=panel):
                found.append((None, round_step, panel))
    return found


def viewer_state(
    user,
    proposal_step=None,
    round_step=None,
    panel=None,
    cache=None,
    request=None,
):
    """The discussion of a proposal's step, or of a round step for a panel,
    as ``user`` sees it: ``{uuid, state, can_open}``, or None when the step
    has no discussion, the user is kept away from it (``kept_away``), or
    neither takes part in it nor oversees the call. ``cache`` is shared by
    the rows of one response; ``request``, when given, holds a personal
    access token to its scopes."""
    if user is None or not user.is_authenticated:
        return None
    call_step = _call_step_of(proposal_step, round_step)
    # Once switched off, the step shows none: its discussions closed then,
    # and their records are read through the discussions themselves.
    if not call_step.has_discussion:
        return None
    try:
        if proposal_step is not None:
            target = target_of(proposal_step, cache)
        else:
            target = round_target(round_step, panel, cache)
    except (DiscussionConflict, exceptions.ValidationError):
        target = None
    key = None
    if target is not None:
        key = (user.pk, *(getattr(item, "pk", None) for item in target))
        if cache is not None and key in cache:
            return cache[key]
    discussion = _find(*target, cache) if target is not None else None
    if _is_kept_away(user, proposal_step, round_step, panel, discussion, cache):
        result = None
    else:
        call = (
            proposal_step.proposal.round.call
            if proposal_step is not None
            else round_step.round.call
        )
        result = _viewer_state(request or user, user, target, discussion, call, cache)
    if key is not None and cache is not None:
        cache[key] = result
    return result


def _viewer_state(who, user, target, discussion, call, cache=None):
    """``discussion`` is the latest (``find``). One closed while the step
    runs again opens anew: ``can_open`` with the closed one's uuid, and
    opening it gives a new discussion.

    Who takes part is the costly question, so it is asked only when the
    answer depends on it: not of someone overseeing the call while the
    step is not running, whom it shows the discussion either way and who
    could not open it."""
    closed = discussion is not None and discussion.closed_at is not None
    state = CLOSED if closed else OPEN if discussion is not None else NOT_OPENED
    result = {
        "uuid": discussion.uuid.hex if discussion is not None else None,
        "state": state,
        "can_open": False,
    }
    oversees = _oversees(who, call, cache)
    running = (
        target is not None and matrix_client.is_enabled() and _is_running(target, cache)
    )
    if oversees and not running:
        return result
    # Who takes part now: in the open one, with what it covered before;
    # else in a new one.
    participant = target is not None and _takes_part(
        user,
        discussion
        if discussion is not None and not closed
        else models.StepDiscussion(
            proposal_step=target[0], round_step=target[1], panel=target[2]
        ),
        cache,
    )
    if not (participant or oversees):
        # For a closed one, who took part reads it too.
        if not (closed and discussion.participants.filter(pk=user.pk).exists()):
            return None
    result["can_open"] = bool(participant and running)
    return result
