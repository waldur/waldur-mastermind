"""Evaluator roles on a proposal: who holds them, and changing who does.

A call defines evaluator roles (``CallProposalRole``, e.g. a lead and a second
rapporteur); people hold them per proposal (``ProposalRoleAssignment``). This
module is the only code that reads those assignments. Permissions, step
completers, notification recipients, step contributors and read access all ask
``proposal_role_holders`` (or, for querysets over many proposals,
``proposals_held_by``), so the people who may act and the people who are told
are always one set.

Holders are evaluators, not the applicant team: nothing here grants a
``UserRole``, so they are never in ``get_users(proposal)``, never get applicant
mail and are never mapped onto the awarded project.

A holder counts once assigned and not revoked, and -- when the assignment
asked for it -- once they have confirmed having no conflict of interest. Until
then the assignment exists, can be revoked, but gives no access. A conflict
of interest recorded later stops it the same way, until it is dismissed or
waived; a recusal revokes it.

One person holds at most one evaluator role on a proposal.
"""

import logging

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from rest_framework import serializers

from waldur_core.core.models import User
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import get_users, has_user
from waldur_mastermind.proposal import (
    coi_declarations,
    models,
    panels,
    reviewer_standing,
)
from waldur_mastermind.proposal.enums import (
    COIStatuses,
    ContributorSources,
    ProposalRoleEligibility,
    ProposalStates,
    ResponsibleRoles,
    StepKinds,
    WorkflowStepInstanceStatuses,
    parse_contributor_source,
)

# How a step or a contribution names a role in configuration strings:
# ``proposal_role:<pk or uuid>``.
ROLE_REF_PREFIX = "proposal_role:"

ELIGIBILITY_TO_CALL_ROLE = {
    ProposalRoleEligibility.CALL_MANAGER: RoleEnum.CALL_MANAGER,
    ProposalRoleEligibility.CALL_REVIEWER: RoleEnum.CALL_REVIEWER,
    ProposalRoleEligibility.CALL_PANEL_MEMBER: RoleEnum.CALL_PANEL_MEMBER,
}

# A recorded conflict stops an assignment until it is dismissed or waived.
BLOCKING_COI_STATUSES = (COIStatuses.PENDING, COIStatuses.RECUSED)

# Proposals in these states are settled: nobody is assigned to evaluate them.
CLOSED_PROPOSAL_STATES = (
    ProposalStates.DRAFT,
    ProposalStates.CANCELED,
    ProposalStates.REJECTED,
    ProposalStates.ACCEPTED,
)

logger = logging.getLogger(__name__)


# -- resolving --------------------------------------------------------------


def resolve_role(call, role_ref):
    """The role of ``call`` that ``role_ref`` names, or None.

    ``role_ref`` is a ``CallProposalRole``, its pk, its uuid, its key or a
    ``proposal_role:<pk or uuid>`` string. None names no role.
    """
    if role_ref is None:
        return None
    roles = models.CallProposalRole.objects.filter(call=call)
    if isinstance(role_ref, models.CallProposalRole):
        return role_ref if role_ref.call_id == call.pk else None
    if isinstance(role_ref, int):
        return roles.filter(pk=role_ref).first()
    ref = str(role_ref)
    if ref.startswith(ROLE_REF_PREFIX):
        ref = ref[len(ROLE_REF_PREFIX) :]
        if ref.isdigit():
            return roles.filter(pk=int(ref)).first()
    if len(ref.replace("-", "")) == 32:
        try:
            role = roles.filter(uuid=ref).first()
        except (ValueError, TypeError):
            role = None
        if role is not None:
            return role
    return roles.filter(key=ref).first()


def _standing_conflict():
    return models.ConflictOfInterest.objects.filter(
        reviewer__user_id=OuterRef("user_id"),
        proposal_id=OuterRef("proposal_id"),
        status__in=BLOCKING_COI_STATUSES,
    )


def with_standing_conflict(assignments):
    """``assignments`` annotated with ``has_standing_conflict``: whether a
    conflict of interest stands between the holder and the proposal."""
    return assignments.annotate(has_standing_conflict=Exists(_standing_conflict()))


def _effective(assignments):
    """Assignments that give their holder the role right now.

    Not revoked, confirmed when confirmation was asked for, with no conflict
    of interest standing between the holder and the proposal -- whenever it
    was recorded -- and not while the holder is on the applicant team: joining
    it is a conflict of its own (see ``revoke_on_joining_team``).
    """
    return (
        assignments.filter(revoked_at__isnull=True)
        .filter(Q(coi_confirmation_required=False) | Q(coi_confirmed_at__isnull=False))
        .exclude(Exists(_standing_conflict()))
        .exclude(on_applicant_team_expression())
    )


def active_assignments(proposal, role_ref=None):
    """The proposal's assignments not revoked, of one role or of all.

    Includes those still waiting for their holder's conflict-of-interest
    confirmation: these count against ``max_holders`` and for the step gate,
    but give no access until confirmed.
    """
    assignments = models.ProposalRoleAssignment.objects.filter(
        proposal=proposal, revoked_at__isnull=True
    )
    if role_ref is not None:
        role = resolve_role(proposal.round.call, role_ref)
        if role is None:
            return assignments.none()
        assignments = assignments.filter(role=role)
    return assignments


def assignment_history(proposal):
    """Every assignment of the proposal, revoked ones included."""
    return models.ProposalRoleAssignment.objects.filter(proposal=proposal)


def proposal_role_holders(proposal, role_ref=None, lead_only=False):
    """Users holding ``role_ref`` (or any evaluator role) on ``proposal``.

    The one resolver for "who holds this role here": step completers, step
    contributors, notification recipients and read access all use it. An
    unknown role resolves to nobody. ``lead_only`` keeps the lead alone,
    on the same terms as any holder: not revoked, no conflict standing.
    """
    if role_ref is not None:
        role = resolve_role(proposal.round.call, role_ref)
        if role is None:
            return User.objects.none()
        assignments = models.ProposalRoleAssignment.objects.filter(
            proposal=proposal, role=role
        )
    else:
        assignments = models.ProposalRoleAssignment.objects.filter(proposal=proposal)
    if lead_only:
        assignments = assignments.filter(is_lead=True)
    return User.objects.filter(
        id__in=_effective(assignments).values_list("user_id", flat=True)
    )


def holds_role(user, proposal, role_ref=None) -> bool:
    if user is None or user.is_anonymous:
        return False
    return proposal_role_holders(proposal, role_ref).filter(pk=user.pk).exists()


def completer_is_lead_only(call_step) -> bool:
    """Whether only the lead of the step's evaluator role completes it.

    A lead-only step completed by the very role whose lead contributes to it
    -- the lead rapporteur writing and completing the consolidated report --
    is the lead's to complete, and to reject the proposal at. The role's
    other holders still work on it, reading what it reads.
    """
    if not call_step.contributors_lead_only:
        return False
    contributing = resolve_role(call_step.call, call_step.contributor_source)
    return lead_only_completion(
        call_step.contributors_lead_only,
        call_step.completer,
        call_step.completer_proposal_role_id,
        contributing.pk if contributing is not None else None,
    )


def lead_only_completion(
    contributors_lead_only, completer, completer_role, contributing_role
) -> bool:
    """``completer_is_lead_only`` from the step's configuration alone.

    ``completer_role`` and ``contributing_role`` name the evaluator roles
    completing and contributing to the step in one vocabulary -- both ids,
    or both keys as a call document has them.
    """
    return bool(
        contributors_lead_only
        and completer == ResponsibleRoles.PROPOSAL_ROLE
        and completer_role is not None
        and contributing_role is not None
        and completer_role == contributing_role
    )


def step_completers(proposal, call_step):
    """Users completing ``call_step`` on ``proposal`` through its evaluator
    role: the role's holders, or its lead alone when
    ``completer_is_lead_only``. Nobody for a step another role completes.

    The one answer for who acts on such a step, and who is told it awaits
    them, so the two never differ.
    """
    if (
        call_step.completer != ResponsibleRoles.PROPOSAL_ROLE
        or call_step.completer_proposal_role_id is None
    ):
        return User.objects.none()
    return proposal_role_holders(
        proposal,
        call_step.completer_proposal_role_id,
        lead_only=completer_is_lead_only(call_step),
    )


def completes_step(user, proposal, call_step) -> bool:
    """Whether ``user`` is one of ``step_completers``."""
    if user is None or user.is_anonymous:
        return False
    return step_completers(proposal, call_step).filter(pk=user.pk).exists()


def proposals_held_by(user):
    """Ids of the proposals ``user`` holds an evaluator role on.

    ``proposal_role_holders`` across every proposal at once, for querysets.
    """
    if user is None or user.is_anonymous:
        return models.ProposalRoleAssignment.objects.none().values_list(
            "proposal_id", flat=True
        )
    return (
        _effective(models.ProposalRoleAssignment.objects.filter(user=user))
        .order_by()
        .values_list("proposal_id", flat=True)
    )


def roles_held_by(user, lead_only=False):
    """``(proposal id, role id)`` of the evaluator roles ``user`` holds now.

    ``holds_role`` across every proposal at once; ``lead_only`` keeps those
    the user leads, as ``proposal_role_holders`` does.
    """
    if user is None or user.is_anonymous:
        return set()
    assignments = models.ProposalRoleAssignment.objects.filter(user=user)
    if lead_only:
        assignments = assignments.filter(is_lead=True)
    return set(_effective(assignments).order_by().values_list("proposal_id", "role_id"))


def assignments_of(user):
    """Every assignment ``user`` was given, revoked ones included."""
    return models.ProposalRoleAssignment.objects.filter(user=user)


def all_assignments():
    """Every assignment on every proposal, revoked ones included -- for
    listings that narrow it down to what the viewer may see."""
    return models.ProposalRoleAssignment.objects.all()


def get_assignment(assignment_uuid):
    """The assignment with ``assignment_uuid``, revoked or not, or None."""
    return all_assignments().filter(uuid=assignment_uuid).first()


def role_is_assigned(role) -> bool:
    """Whether ``role`` was ever assigned on a proposal, revoked ones
    included: such a role is history and cannot be deleted."""
    return models.ProposalRoleAssignment.objects.filter(role=role).exists()


# -- eligibility and conflicts -----------------------------------------------


def eligible_users(role, proposal=None):
    """Users who may hold ``role`` by its ``eligible_from``, on ``proposal``
    when given.

    The reviewers of a proposal are the call's reviewers who submitted a
    review of it; without a proposal, the call's reviewers. A role held
    through the proposal's panel is for its chair or its members; without a
    proposal, or for one with no panel, for nobody.
    """
    if panels.is_panel_based(role):
        return panels.eligible_users(role, proposal)
    if role.eligible_from == ProposalRoleEligibility.PROPOSAL_REVIEWERS:
        users = get_users(role.call, role_name=RoleEnum.CALL_REVIEWER).filter(
            is_active=True
        )
        if proposal is None:
            return users
        return users.filter(
            id__in=models.Review.objects.filter(
                proposal=proposal, state=models.Review.States.SUBMITTED
            ).values_list("reviewer_id", flat=True)
        )
    call_role = ELIGIBILITY_TO_CALL_ROLE.get(role.eligible_from)
    if call_role is not None:
        return get_users(role.call, role_name=call_role).filter(is_active=True)
    if role.eligible_from == ProposalRoleEligibility.ANY_USER:
        return User.objects.filter(is_active=True)
    return User.objects.none()


def is_eligible(user, role, proposal=None) -> bool:
    return eligible_users(role, proposal).filter(pk=user.pk).exists()


def depends_on_proposal(role) -> bool:
    """Whether who may hold ``role`` is known only per proposal."""
    return (
        role.eligible_from == ProposalRoleEligibility.PROPOSAL_REVIEWERS
        or panels.is_panel_based(role)
    )


# -- the applicant team --------------------------------------------------------
#
# The applicant team is the proposal's team as it stands: everyone holding an
# active role on the proposal (its managers, administrators and members). The
# author is on it only while they still hold one of those roles -- removed
# from the team, they are nobody to the proposal. Nothing grants access
# through ``created_by``.


def is_on_applicant_team(user, proposal) -> bool:
    """Whether ``user`` is on the proposal's team now."""
    if user is None or user.is_anonymous:
        return False
    return has_user(proposal, user)


def answers_for_applicant(user, instance) -> bool:
    """Whether ``user`` completing ``instance`` answers the applicant's own
    step in their place: a step the applicant completes, by someone off
    the applicant team. A step of the applicant's kind that the call gives
    its own team to complete is theirs to complete, not an answer for the
    applicant."""
    call_step = instance.call_step
    return (
        call_step.kind == StepKinds.APPLICANT_ACTION
        and call_step.completer == ResponsibleRoles.APPLICANT
        and not is_on_applicant_team(user, instance.proposal)
    )


def applicant_team_user_ids(proposal) -> set[int]:
    """Ids of everyone on the proposal's team now."""
    return set(get_users(proposal).values_list("id", flat=True))


def applicant_team_proposal_ids(user):
    """Ids of the proposals ``user`` is on the team of, for querysets."""
    return (
        UserRole.objects.filter(
            user=user,
            is_active=True,
            content_type__app_label="proposal",
            content_type__model="proposal",
        )
        .order_by()
        .values_list("object_id", flat=True)
    )


def on_applicant_team_expression(proposal_ref="proposal_id", user_ref="user_id"):
    """``Exists`` over a team role held by the row's user on the row's
    proposal, both named by the outer query's fields."""
    return Exists(
        UserRole.objects.filter(
            # Matched by natural key rather than a ContentType lookup, so
            # building the queryset touches no database.
            content_type__app_label="proposal",
            content_type__model="proposal",
            object_id=OuterRef(proposal_ref),
            user_id=OuterRef(user_ref),
            is_active=True,
        )
    )


def has_reviewer_profile(user) -> bool:
    return models.ReviewerProfile.objects.filter(user=user).exists()


def recorded_conflicts(user, proposal):
    """Conflicts of interest recorded between ``user`` and ``proposal`` that
    still stand. Recorded against a reviewer profile, so a user without one
    has none on record."""
    return models.ConflictOfInterest.objects.filter(
        reviewer__user=user,
        proposal=proposal,
        status__in=BLOCKING_COI_STATUSES,
    )


def users_with_recorded_conflicts(proposal):
    return set(
        models.ConflictOfInterest.objects.filter(
            proposal=proposal, status__in=BLOCKING_COI_STATUSES
        ).values_list("reviewer__user_id", flat=True)
    )


# -- the assignment step -----------------------------------------------------


def coi_confirmation_required(role, assigned_in=None) -> bool:
    """Whether a new holder of ``role`` confirms having no conflict first.

    The step the holder is assigned in decides; outside a step, any enabled
    assignment step of the call for the role that asks for it does.
    """
    if assigned_in is not None:
        return assigned_in.call_step.requires_coi_confirmation
    return models.CallWorkflowStep.objects.filter(
        call_id=role.call_id,
        kind=StepKinds.ASSIGNMENT,
        assigns_roles=role,
        is_enabled=True,
        requires_coi_confirmation=True,
    ).exists()


def active_assignment_step(proposal, role=None):
    """The proposal's active assignment step (assigning ``role`` among its
    roles, when given), else None. Of several steps running at once, the
    first such."""
    instances = (
        proposal.workflow_step_instances.filter(
            status=WorkflowStepInstanceStatuses.ACTIVE,
            call_step__kind=StepKinds.ASSIGNMENT,
        )
        .select_related("call_step")
        .order_by("order", "id")
    )
    for instance in instances:
        roles = instance.call_step.assigns_roles.all()
        if role is not None:
            roles = roles.filter(pk=role.pk)
        if roles.exists():
            return instance
    return None


def active_instances_of_kind(proposal, kind):
    """The proposal's active step instances of ``kind``, in path order."""
    return (
        proposal.workflow_step_instances.filter(
            status=WorkflowStepInstanceStatuses.ACTIVE, call_step__kind=kind
        )
        .select_related("call_step", "proposal")
        .order_by("order", "id")
    )


def holder_blocked_by(assignment) -> str:
    """Why the holder of ``assignment`` cannot work on the proposal yet, as
    the end of a sentence naming them, with what to do about it.

    ``assignment`` is annotated by ``with_standing_conflict``. A conflict
    recorded against the holder stops them until it is dismissed or waived:
    the way forward is to replace them. A holder who has not confirmed
    having no conflict only has to confirm.
    """
    if assignment.has_standing_conflict:
        return (
            "has a recorded conflict of interest with this proposal: replace "
            "them, or dismiss or waive the conflict."
        )
    if assignment.coi_confirmation_required and not assignment.coi_confirmed_at:
        return "has not yet confirmed having no conflict of interest."
    return "cannot work on this proposal now."


def missing_lead(proposal, call_step) -> str | None:
    """Why a step only the lead contributes to waits, or None when it need
    not: the evaluator role it names has no lead able to contribute."""
    if not call_step.contributors_lead_only:
        return None
    role = resolve_role(proposal.round.call, call_step.contributor_source)
    if role is None:
        return None
    if proposal_role_holders(proposal, role, lead_only=True).exists():
        return None
    lead = (
        with_standing_conflict(active_assignments(proposal, role))
        .filter(is_lead=True)
        .first()
    )
    if lead is not None:
        return f"The lead of the role '{role.name}' {holder_blocked_by(lead)}"
    return (
        f"Only the lead of the role '{role.name}' contributes to this step; "
        "mark one holder as the lead."
    )


def missing_holders(proposal, call_step) -> str | None:
    """Why an assignment step cannot complete yet, or None when it can.

    Each of the step's roles needs at least ``min_holders`` holders and, when
    it has a lead, the lead marked.
    """
    if call_step.kind != StepKinds.ASSIGNMENT:
        return None
    for role in call_step.assigns_roles.all():
        assignments = active_assignments(proposal, role)
        count = assignments.count()
        if count < role.min_holders and panels.is_panel_based(role):
            reason = _panel_staffing_gap(proposal, role)
            if reason:
                return reason
        if (
            count < role.min_holders
            and depends_on_proposal(role)
            and not eligible_users(role, proposal).exists()
        ):
            return (
                f"The role '{role.name}' is for the reviewers of the proposal, "
                "and no reviewer has submitted a review of it yet."
            )
        if count < role.min_holders:
            return (
                f"{count} of {role.min_holders} required holders of the role "
                f"'{role.name}' are assigned."
            )
        if role.allows_lead and not assignments.filter(is_lead=True).exists():
            return f"Mark one holder of the role '{role.name}' as the lead."
    return None


def _panel_staffing_gap(proposal, role) -> str | None:
    """Why the proposal's panel cannot give ``role`` the holders it needs,
    with what to do about it, or None when it can."""
    panel = panels.panel_of(proposal)
    if panel is None:
        return (
            f"The role '{role.name}' is held through the proposal's panel, and "
            "the proposal has none: set its panel."
        )
    available = eligible_users(role, proposal).exclude(
        id__in=applicant_team_user_ids(proposal)
    )
    available = available.exclude(id__in=users_with_recorded_conflicts(proposal))
    if role.eligible_from == ProposalRoleEligibility.PANEL_CHAIR:
        if panels.chair_of_panel(panel) is None:
            return (
                f"The panel '{panel.name}' has no chair: set its chair, or move "
                "the proposal to another panel."
            )
        if not available.exists() and not active_assignments(proposal, role).exists():
            return (
                f"The chair of the panel '{panel.name}' cannot take this "
                "proposal (a conflict of interest, or the applicant team): move "
                "the proposal to another panel, or change the panel's chair."
            )
        return None
    held = active_assignments(proposal, role).values_list("user_id", flat=True)
    if available.exclude(id__in=held).count() + held.count() < role.min_holders:
        return (
            f"The panel '{panel.name}' has too few members able to take this "
            f"proposal for the {role.min_holders} required holders of the role "
            f"'{role.name}': add members to the panel, or move the proposal to "
            "another panel."
        )
    return None


# -- changing holders --------------------------------------------------------


def lock_role_holders(proposal, role):
    """Lock the proposal, then ``role``, before its holders change: they are
    counted against max_holders and the lead is moved between them, so two
    changes at once would both see room for one more, and a step completing
    meanwhile would count holders about to go. The proposal first, as every
    step action takes it. Returns the proposal as locked."""
    locked = models.Proposal.objects.select_for_update(of=("self",)).get(pk=proposal.pk)
    models.CallProposalRole.objects.select_for_update().filter(pk=role.pk).first()
    return locked


@transaction.atomic
def assign(
    proposal,
    role,
    user,
    *,
    assigned_by,
    assigned_in=None,
    is_lead=False,
    discloses_conflicts=True,
):
    """Make ``user`` a holder of ``role`` on ``proposal``.

    Refused with a ``ValidationError`` for a role of another call, a draft or
    settled proposal, someone on the applicant team, an ineligible user, a
    user with a recorded conflict of interest, one holder too many, a lead the
    role does not have, or someone already holding an evaluator role on the
    proposal. A lead replaces the previous one.

    Without ``discloses_conflicts`` -- for whoever may not read who is
    conflicted with the proposal -- a recorded conflict is refused as an
    ineligible user is, so the refusal does not tell one from the other.
    """
    if role.call_id != proposal.round.call_id:
        raise serializers.ValidationError({"role": "The role belongs to another call."})
    if proposal.state == ProposalStates.DRAFT:
        raise serializers.ValidationError(
            "Evaluators are assigned once the proposal is submitted."
        )
    if proposal.state in CLOSED_PROPOSAL_STATES:
        raise serializers.ValidationError(
            f"The proposal is {proposal.get_state_display().lower()}; "
            "evaluators are no longer assigned."
        )
    lock_role_holders(proposal, role)
    if is_on_applicant_team(user, proposal):
        raise serializers.ValidationError(
            {"user": "A member of the applicant team cannot evaluate the proposal."}
        )
    # A reviewer the operator has suspended or blocked takes on no evaluation.
    standing_error = reviewer_standing.standing_error(user)
    if standing_error:
        raise serializers.ValidationError({"user": standing_error})
    if not discloses_conflicts and (
        not is_eligible(user, role, proposal)
        or recorded_conflicts(user, proposal).exists()
    ):
        raise serializers.ValidationError(
            {
                "user": (
                    f"The user may not hold the role '{role.name}' on this "
                    f"proposal; it is for {role.get_eligible_from_display().lower()}."
                )
            }
        )
    if not is_eligible(user, role, proposal):
        raise serializers.ValidationError(
            {
                "user": (
                    f"The user may not hold the role '{role.name}': it is for "
                    f"{role.get_eligible_from_display().lower()}."
                )
            }
        )
    if recorded_conflicts(user, proposal).exists():
        raise serializers.ValidationError(
            {"user": "A conflict of interest is recorded for this user and proposal."}
        )
    held = active_assignments(proposal).filter(user=user).select_related("role")
    held = held.first()
    if held is not None:
        if held.role_id == role.pk:
            message = f"The user already holds the role '{role.name}'."
        else:
            message = (
                f"The user already holds the role '{held.role.name}' on this "
                "proposal; one person holds one evaluator role per proposal."
            )
        raise serializers.ValidationError({"user": message})
    assignments = active_assignments(proposal, role)
    if role.max_holders is not None and assignments.count() >= role.max_holders:
        raise serializers.ValidationError(
            {
                "user": (
                    f"The role '{role.name}' already has its {role.max_holders} "
                    f"holder{'s' if role.max_holders != 1 else ''}."
                )
            }
        )
    if is_lead and not role.allows_lead:
        raise serializers.ValidationError(
            {"is_lead": f"The role '{role.name}' has no lead."}
        )
    if is_lead:
        assignments.filter(is_lead=True).update(is_lead=False)
    return models.ProposalRoleAssignment.objects.create(
        proposal=proposal,
        role=role,
        user=user,
        is_lead=is_lead,
        assigned_by=assigned_by,
        assigned_in=assigned_in,
        coi_confirmation_required=coi_confirmation_required(role, assigned_in),
        coi_declaration_only=not has_reviewer_profile(user),
    )


@transaction.atomic
def revoke(assignment, revoked_by, reason=""):
    """Take the role away; the row stays as history, with ``reason`` when it
    was not taken away by hand. Under the role holders' lock, as assigning
    and moving the lead are."""
    lock_role_holders(assignment.proposal, assignment.role)
    assignment = models.ProposalRoleAssignment.objects.select_for_update().get(
        pk=assignment.pk
    )
    if assignment.revoked_at is not None:
        raise serializers.ValidationError("The assignment is already revoked.")
    assignment.revoked_at = timezone.now()
    assignment.revoked_by = revoked_by
    assignment.revoked_reason = reason
    assignment.is_lead = False
    assignment.save(
        update_fields=[
            "revoked_at",
            "revoked_by",
            "revoked_reason",
            "is_lead",
            "modified",
        ]
    )
    return assignment


def assign_panel_chairs(instance):
    """Assign the proposal's panel chair to each role of the assignment step
    ``instance`` held by the chair of the proposal's panel, as the step
    starts.

    For roles of at most one holder that have none yet. Through ``assign``,
    so the chair confirms having no conflict of interest before they gain
    access when the step asks for it, and a chair who may not take the
    proposal -- on its applicant team, a conflict on record -- is not
    assigned: the step then waits, and says why (``missing_holders``).
    Returns the assignments made.
    """
    call_step = instance.call_step
    if call_step.kind != StepKinds.ASSIGNMENT:
        return []
    proposal = instance.proposal
    chair = panels.chair_of(proposal)
    if chair is None:
        return []
    made = []
    for role in call_step.assigns_roles.filter(
        eligible_from=ProposalRoleEligibility.PANEL_CHAIR
    ):
        if role.max_holders is None or role.max_holders > 1:
            continue
        if active_assignments(proposal, role).exists():
            continue
        try:
            assignment = assign(
                proposal, role, chair, assigned_by=None, assigned_in=instance
            )
        except serializers.ValidationError as error:
            logger.info(
                "Panel chair %s not assigned as %s on proposal %s: %s",
                chair.uuid.hex,
                role.key,
                proposal.uuid.hex,
                error.detail,
            )
            continue
        made.append(assignment)
    return made


RECUSAL_REASON = "Recused for a conflict of interest."


def revoke_on_recusal(user, proposal, revoked_by):
    """Revoke ``user``'s evaluator roles on ``proposal`` once they recuse
    themselves from it for a conflict of interest. Returns the revoked
    assignments."""
    revoked = [
        revoke(assignment, revoked_by, reason=RECUSAL_REASON)
        for assignment in active_assignments(proposal).filter(user=user)
    ]
    for assignment in revoked:
        logger.info(
            "Evaluator role assignment %s on proposal %s revoked: the holder "
            "recused for a conflict of interest.",
            assignment.uuid.hex,
            proposal.uuid.hex,
        )
    return revoked


LOST_ELIGIBILITY_REASON = "No longer eligible for the role."


def revoke_no_longer_eligible(call, user_ids, revoked_by=None):
    """Revoke the evaluator roles of the people ``user_ids`` on ``call``'s
    proposals that they may no longer hold by the role's ``eligible_from``:
    they left the call role, or the panel, they held it through. A role open
    to any user is never lost this way. Returns the revoked assignments."""
    assignments = (
        models.ProposalRoleAssignment.objects.filter(
            proposal__round__call=call,
            user_id__in=user_ids,
            revoked_at__isnull=True,
        )
        .exclude(role__eligible_from=ProposalRoleEligibility.ANY_USER)
        .select_related("role__call", "proposal__panel", "user")
        .order_by("id")
    )
    revoked = []
    for assignment in assignments:
        if is_eligible(assignment.user, assignment.role, assignment.proposal):
            continue
        revoked.append(revoke(assignment, revoked_by, reason=LOST_ELIGIBILITY_REASON))
        logger.info(
            "Evaluator role assignment %s on proposal %s revoked: the holder "
            "is no longer eligible for the role.",
            assignment.uuid.hex,
            assignment.proposal.uuid.hex,
        )
    return revoked


JOINED_TEAM_REASON = "Joined the applicant team."


def revoke_on_joining_team(user, proposal, revoked_by):
    """Revoke ``user``'s evaluator roles on ``proposal`` once they join its
    applicant team: nobody evaluates a proposal they apply with. Returns the
    revoked assignments."""
    revoked = [
        revoke(assignment, revoked_by, reason=JOINED_TEAM_REASON)
        for assignment in active_assignments(proposal).filter(user=user)
    ]
    for assignment in revoked:
        logger.info(
            "Evaluator role assignment %s on proposal %s revoked: the holder "
            "joined the applicant team.",
            assignment.uuid.hex,
            proposal.uuid.hex,
        )
    return revoked


@transaction.atomic
def set_lead(assignment):
    """Mark the holder as the role's lead on the proposal, in place of any
    other."""
    lock_role_holders(assignment.proposal, assignment.role)
    assignment = models.ProposalRoleAssignment.objects.get(pk=assignment.pk)
    if assignment.revoked_at is not None:
        raise serializers.ValidationError("The assignment is revoked.")
    if not assignment.role.allows_lead:
        raise serializers.ValidationError(
            {"is_lead": f"The role '{assignment.role.name}' has no lead."}
        )
    active_assignments(assignment.proposal, assignment.role).filter(
        is_lead=True
    ).exclude(pk=assignment.pk).update(is_lead=False)
    assignment.is_lead = True
    assignment.save(update_fields=["is_lead", "modified"])
    return assignment


@transaction.atomic
def confirm_no_conflict(assignment):
    """The holder declares having no conflict of interest with the proposal,
    in the words of the declaration they were shown, which the assignment
    keeps (``coi_declarations``).

    Refused while a conflict is on record. A holder without a reviewer
    profile cannot be checked against recorded conflicts at all, which the
    assignment records.
    """
    assignment = models.ProposalRoleAssignment.objects.select_for_update().get(
        pk=assignment.pk
    )
    if assignment.revoked_at is not None:
        raise serializers.ValidationError("The assignment is revoked.")
    if assignment.coi_confirmed_at is not None:
        raise serializers.ValidationError("Already confirmed.")
    if recorded_conflicts(assignment.user, assignment.proposal).exists():
        raise serializers.ValidationError(
            "A conflict of interest is recorded for you and this proposal; "
            "ask the call manager to resolve it."
        )
    assignment.coi_confirmed_at = timezone.now()
    assignment.coi_declaration_text = coi_declarations.for_assignment(assignment)
    assignment.coi_declaration_only = not has_reviewer_profile(assignment.user)
    assignment.save(
        update_fields=[
            "coi_confirmed_at",
            "coi_declaration_text",
            "coi_declaration_only",
            "modified",
        ]
    )
    return assignment


# -- whether the call has enough people ------------------------------------------

# The people a call role names, singular and plural, as the Team tab lists them.
_POOL_NOUNS = {
    RoleEnum.CALL_MANAGER: ("call manager", "call managers"),
    RoleEnum.CALL_REVIEWER: ("reviewer", "reviewers"),
    RoleEnum.CALL_PANEL_MEMBER: ("panel member", "panel members"),
}

# A step's ``call_role:<role>`` contributor source, as the call role it names.
CONTRIBUTOR_CALL_ROLES = {
    "call_manager": RoleEnum.CALL_MANAGER,
    "reviewer": RoleEnum.CALL_REVIEWER,
    "panel_member": RoleEnum.CALL_PANEL_MEMBER,
}


def eligible_count(role):
    """How many people may hold ``role`` on the call now; None when anyone
    may. For a role held through the proposal's panel, how many may on the
    call's smallest panel."""
    if panels.is_panel_based(role):
        return panels.smallest_panel(role.call, role.eligible_from)[1]
    if role.eligible_from == ProposalRoleEligibility.ANY_USER or (
        depends_on_proposal(role)
    ):
        return None
    return eligible_users(role).count()


def _call_role_shortfall(count, needed, role_name, what):
    if count >= needed:
        return None
    singular, plural = _POOL_NOUNS[role_name]
    have = (
        f"no {plural}"
        if count == 0
        else f"only {count} {singular if count == 1 else plural}"
    )
    return f"The call has {have}, but {what}: add {plural} in the call's Team tab."


def holders_shortfall(role, needed=None):
    """Why the call cannot staff ``role`` on a proposal -- fewer people may
    hold it than ``needed``, its minimum of holders by default -- or None."""
    needed = needed or role.min_holders or 0
    if panels.is_panel_based(role) and needed:
        return _panel_shortfall(role, needed)
    call_role = ELIGIBILITY_TO_CALL_ROLE.get(role.eligible_from)
    if call_role is None or not needed:
        return None
    return _call_role_shortfall(
        eligible_users(role).count(),
        needed,
        call_role,
        f"each proposal needs {needed} "
        f"{'holder' if needed == 1 else 'holders'} of the role '{role.name}'",
    )


def _panel_shortfall(role, needed):
    """Why some panel of the call cannot staff ``role``, held through the
    proposal's panel, on its proposals, or None."""
    panel, count = panels.smallest_panel(role.call, role.eligible_from)
    if panel is None:
        return (
            f"The role '{role.name}' is held through the proposal's panel, but "
            "the call has no panels: add them in the call's Panels tab."
        )
    if count >= needed:
        return None
    if role.eligible_from == ProposalRoleEligibility.PANEL_CHAIR:
        return (
            f"The panel '{panel.name}' has no chair, but each of its proposals "
            f"needs its chair as the role '{role.name}': set one in the call's "
            "Panels tab."
        )
    have = (
        "no members"
        if count == 0
        else f"only {count} member{'s' if count != 1 else ''}"
    )
    return (
        f"The panel '{panel.name}' has {have}, but each proposal needs {needed} "
        f"{'holder' if needed == 1 else 'holders'} of the role '{role.name}': "
        "add members in the call's Panels tab."
    )


def contributors_shortfall(call_step):
    """Why the call cannot give the step the contributions it needs, or
    None: the evaluator role or the call role it takes them from has fewer
    people than its minimum."""
    try:
        source, parameter = parse_contributor_source(call_step.contributor_source)
    except ValueError:
        return None
    needed = call_step.min_contributions or 0
    if source == ContributorSources.PROPOSAL_ROLE:
        role = resolve_role(call_step.call, call_step.contributor_source)
        if role is None:
            return None
        return holders_shortfall(role, max(needed, role.min_holders or 0))
    if (
        source == ContributorSources.CALL_ROLE
        and parameter == ResponsibleRoles.PANEL_CHAIR
    ):
        call = call_step.call
        seated = len({call.panel_chair_id, call.panel_vice_chair_id} - {None})
        if seated >= needed:
            return None
        have = (
            "neither a panel chair nor a vice-chair"
            if not seated
            else "only one of a panel chair and a vice-chair"
        )
        return (
            f"The call has {have}, but the step needs {needed} "
            f"{'contribution' if needed == 1 else 'contributions'} from the "
            "panel chair and vice-chair: set them in the call's Team tab."
        )
    if source == ContributorSources.CALL_ROLE and needed:
        role_name = CONTRIBUTOR_CALL_ROLES[parameter]
        return _call_role_shortfall(
            get_users(call_step.call, role_name=role_name).count(),
            needed,
            role_name,
            f"the step needs {needed} "
            f"{'contribution' if needed == 1 else 'contributions'}",
        )
    if source == ContributorSources.ASSIGNED_REVIEWERS and needed:
        # Only reviewers who may still be given reviews count.
        accepted = (
            models.CallReviewerPool.objects.eligible()
            .filter(call=call_step.call)
            .count()
        )
        if accepted < needed:
            have = "no reviewer" if accepted == 0 else f"only {accepted}"
            return (
                f"The step needs {needed} reviews per proposal, but {have} "
                f"{'has' if accepted < 2 else 'have'} accepted to review for "
                "the call: invite more reviewers to the call's reviewer pool."
            )
    return None
