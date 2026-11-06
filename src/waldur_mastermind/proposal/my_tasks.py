"""The open workflow work of one user, across every call.

Lists what the user must do next: fill a contribution, complete a step,
answer an assignment or an invitation, write a review, respond for the
applicant, confirm having no conflict of interest, release a report or
advance a parked workflow, or answer a request to include an offering the
user's provider side offers in a call.

Each kind of task follows the rule that gates acting on it, so the list never
offers what the proposal page would refuse:

- a contribution slot -- ``contributions.may_contribute`` and an open slot
  the user fills (``Access.can_fill``), or may open (``_may_open_own_slot``);
- completing a step -- the step's completer role as
  ``permissions.user_completes_step`` reads it, and for a step the call
  manager completes ``permissions.manages_call_workflow``;
- releasing a report -- ``step_inputs.can_release``;
- advancing -- ``workflow_service.is_awaiting_manual_advance`` and
  ``permissions.can_advance_workflow_step``.

Those helpers answer for one proposal at a time. Here the same rules are
evaluated against the user's roles loaded once, so the list costs a fixed
number of queries however many tasks it holds. Staff see their own tasks,
not everyone's: the staff bypass of the gates is not work waiting for them.
"""

import dataclasses
import datetime

from django.contrib.contenttypes.models import ContentType
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from waldur_core.configuration import config
from waldur_core.permissions import models as permissions_models
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.structure.managers import get_connected_customers_by_permission
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.managers import (
    get_connected_offerings_by_permission,
)
from waldur_mastermind.proposal import (
    change_requests,
    coi_declarations,
    contributions,
    models,
    proposal_roles,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    AWARD_RESPONSE_KEY,
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    CallStates,
    ContributorSources,
    ProposalRunStates,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    ReviewerPoolInvitationStatuses,
    StepContributionStatuses,
    StepKinds,
    TransitionModes,
    WorkflowStepInstanceStatuses,
    parse_contributor_source,
)

Statuses = StepContributionStatuses

# A proposal whose workflow is still running.
OPEN_PROPOSAL_STATES = (ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW)


class Needs:
    """What a task asks of the user."""

    CONTRIBUTE = "contribute"
    REVIEW = "review"
    COMPLETE = "complete"
    ASSIGN = "assign"
    DECIDE = "decide"
    RESPOND = "respond"
    ACCEPT_AWARD = "accept_award"
    ADVANCE = "advance"
    RELEASE_REPORT = "release_report"
    CONFIRM_NO_COI = "confirm_no_coi"
    ANSWER_ASSIGNMENT = "answer_assignment"
    ANSWER_INVITATION = "answer_invitation"
    ACCEPT_DELEGATION = "accept_delegation"
    SUBMIT_CHANGES = "submit_changes"
    WRITE_ROUND_REPORT = "write_round_report"
    OPEN_ROUND_STEP = "open_round_step"
    COMPLETE_ROUND_STEP = "complete_round_step"
    ANSWER_OFFERING_REQUEST = "answer_offering_request"

    CHOICES = (
        (CONTRIBUTE, "Submit your contribution"),
        (REVIEW, "Write your review"),
        (COMPLETE, "Complete the step"),
        (ASSIGN, "Assign evaluators"),
        (DECIDE, "Record the decision"),
        (RESPOND, "Respond for the applicant"),
        (ACCEPT_AWARD, "Accept or decline the award"),
        (ADVANCE, "Advance the workflow"),
        (RELEASE_REPORT, "Release the report to the applicant"),
        (CONFIRM_NO_COI, "Confirm you have no conflict of interest"),
        (ANSWER_ASSIGNMENT, "Accept or decline the assignment"),
        (ANSWER_INVITATION, "Accept or decline the invitation"),
        (ACCEPT_DELEGATION, "Accept or decline the contribution handed to you"),
        (SUBMIT_CHANGES, "Make and submit the changes requested"),
        (WRITE_ROUND_REPORT, "Write and submit your round report"),
        (OPEN_ROUND_STEP, "Open the step for the round"),
        (COMPLETE_ROUND_STEP, "Complete the step for the round"),
        (ANSWER_OFFERING_REQUEST, "Accept or reject the request for your offering"),
    )
    LABELS = dict(CHOICES)


class Targets:
    """Where the UI opens a task."""

    PROPOSAL = "proposal"
    REVIEW = "review"
    ASSIGNMENTS = "assignments"
    INVITATIONS = "invitations"
    EVALUATOR_ROLES = "evaluator_roles"
    ROUND_REPORT = "round_report"
    ROUND = "round"
    OFFERING_REQUESTS = "offering_requests"

    CHOICES = (
        (PROPOSAL, "The proposal's page"),
        (REVIEW, "The review's page"),
        (ASSIGNMENTS, "The user's assignment batches"),
        (INVITATIONS, "The user's reviewer pool invitations"),
        (EVALUATOR_ROLES, "The user's evaluator roles"),
        (ROUND_REPORT, "The round report's page"),
        (ROUND, "The round on its call's page"),
        (OFFERING_REQUESTS, "The provider's requests for offerings"),
    )


class Views:
    """Whose view of the proposal page a task opens: the call team's, run
    by the organisation managing the call; the offering team's, with its
    provider's chain back; the applicant's; or, for an evaluator or someone
    a slot was handed to, the plain page."""

    CALL_TEAM = "call_team"
    OFFERING_TEAM = "offering_team"
    APPLICANT = "applicant"
    PARTICIPANT = "participant"

    CHOICES = (
        (CALL_TEAM, "The call team's view"),
        (OFFERING_TEAM, "The offering team's view"),
        (APPLICANT, "The applicant's view"),
        (PARTICIPANT, "The proposal page of an evaluator or delegate"),
    )


# Section ids of the proposal page.
ALLOCATION_SECTION = "step-awarded-resources"
# The panel with the current step's actions: completing, deciding.
STEP_ACTIONS_SECTION = "step-current-actions"
CHECKLIST_SECTION = "step-checklist"
EVALUATORS_SECTION = "step-evaluators"
RELEASED_REPORTS_SECTION = "step-released-reports"


# Why a call manager is given work that is someone else's.
STAND_IN_SLOT = "{offering}: nobody is on its team, assess it in its place"
STAND_IN_COMPLETER = "Nobody holds the {role} role, complete it in its place"


def contributions_section(call_step):
    return f"step-contributions-{call_step.uuid.hex}"


def response_section(call_step):
    """Where the applicant answers a step of theirs that reads earlier
    steps, such as a response to reviews."""
    return f"step-response-{call_step.uuid.hex}"


@dataclasses.dataclass
class Task:
    key: str
    needed: str
    target: str
    proposal: models.Proposal | None = None
    call: models.Call | None = None
    call_round: models.Round | None = None
    instance: models.ProposalWorkflowStepInstance | None = None
    due_at: datetime.datetime | None = None
    section: str | None = None
    target_uuid: str | None = None
    detail: str = ""
    # The conflict-of-interest declaration the task has the user confirm.
    coi_declaration_text: str = ""
    # The offering the task is about, where it is about one: the offering a
    # per-offering slot assesses, the one whose managers complete the step,
    # or the one a request asks the provider to include in a call.
    offering: marketplace_models.Offering | None = None
    # Whose view of the proposal page opens it, and the organisation that
    # view is under, for a task on a proposal (see ``Views``).
    view: str | None = None
    # The uuid of the organisation that view is under.
    view_customer: str | None = None

    @property
    def needed_label(self) -> str:
        return Needs.LABELS[self.needed]

    @property
    def is_overdue(self) -> bool:
        return self.due_at is not None and self.due_at < timezone.now()


# =============================================================================
# The user's standing, loaded once
# =============================================================================


def _role_scope_ids(user, model, permission=None, role_names=None):
    roles = permissions_models.UserRole.objects.filter(
        user=user,
        is_active=True,
        content_type=ContentType.objects.get_for_model(model),
    )
    if permission:
        roles = roles.filter(role__permissions__permission=permission)
    if role_names:
        roles = roles.filter(
            Q(role__name__in=role_names) | Q(role__template__name__in=role_names)
        )
    return roles.order_by().values_list("object_id", flat=True).distinct()


class Standing:
    """The user's roles that decide what workflow work is theirs."""

    def __init__(self, user):
        self.user = user
        self.team_proposal_ids = set(proposal_roles.applicant_team_proposal_ids(user))
        # Proposals a conflict of interest of the user's stands with: no
        # call role but the call manager's holds on them.
        self.conflicted_proposal_ids = set(
            models.ConflictOfInterest.objects.filter(
                reviewer__user=user,
                proposal__isnull=False,
                status__in=proposal_roles.BLOCKING_COI_STATUSES,
            ).values_list("proposal_id", flat=True)
        )
        self.managed_proposal_ids = set(
            permissions_models.UserRole.objects.filter(
                user=user,
                is_active=True,
                role__name=RoleEnum.PROPOSAL_MANAGER,
                content_type=ContentType.objects.get_for_model(models.Proposal),
            ).values_list("object_id", flat=True)
        )
        self.managed_offering_ids = set(contributions.offering_ids_managed_by(user))
        # Every offering the user holds a role on: its managers, and those
        # its part of a step can be handed to.
        self.offering_team_ids = set(contributions.offering_ids_with_role_of(user))
        # Call id -> the call roles held on it, by name and by template name,
        # as ``get_users(call, role_name=...)`` matches them.
        self.call_roles: dict[int, set[str]] = {}
        for call_id, name, template in permissions_models.UserRole.objects.filter(
            user=user,
            is_active=True,
            content_type=ContentType.objects.get_for_model(models.Call),
        ).values_list("object_id", "role__name", "role__template__name"):
            held = self.call_roles.setdefault(call_id, set())
            held.update(filter(None, (name, template)))
        # Managing organisations whose calls the user drives as organiser.
        self.organiser_ids = set(
            _role_scope_ids(
                user,
                models.CallManagingOrganisation,
                permission=PermissionEnum.APPROVE_AND_REJECT_PROPOSALS,
            )
        )
        # Calls, and managing organisations, the user may update: those
        # overseeing a call release its reports.
        self.updatable_call_ids = set(
            _role_scope_ids(user, models.Call, permission=PermissionEnum.UPDATE_CALL)
        )
        self.updatable_organisation_ids = set(
            _role_scope_ids(
                user,
                models.CallManagingOrganisation,
                permission=PermissionEnum.UPDATE_CALL,
            )
        )
        # (proposal id, role id) of the evaluator roles the user holds now.
        self.held_roles = proposal_roles.roles_held_by(user)
        self.led_roles = proposal_roles.roles_held_by(user, lead_only=True)
        self.held_role_proposal_ids = {
            proposal_id for proposal_id, _ in self.held_roles
        }
        # Filled per batch of candidate steps.
        self.requested_offering_ids: dict[int, set[int]] = {}
        self.roles_by_call: dict[int, list] = {}
        # (proposal id, role id) of the evaluator roles someone holds.
        self.assigned_roles: set[tuple[int, int]] = set()
        # Offerings with someone on their team, and the call roles somebody
        # holds on each call: where nobody does, the call's managers act in
        # their place.
        self.staffed_offering_ids: set[int] = set()
        self.held_call_roles: dict[int, set[str]] = {}
        self._offerings: dict[int, marketplace_models.Offering] = {}

    def offering(self, offering_id):
        """The offering, with its provider, loaded once."""
        if offering_id not in self._offerings:
            self._offerings[offering_id] = (
                marketplace_models.Offering.objects.select_related("customer")
                .filter(pk=offering_id)
                .first()
            )
        return self._offerings[offering_id]

    def managed_requested_offering(self, proposal):
        """The offering of the user's own the proposal requests, when the
        user manages one; the first of them when several."""
        managed = (
            self.requested_offering_ids.get(proposal.pk, set())
            & self.managed_offering_ids
        )
        return self.offering(min(managed)) if managed else None

    # --- per call ------------------------------------------------------------

    def holds_call_role(self, call, role_name) -> bool:
        return role_name in self.call_roles.get(call.pk, ())

    def manages_call_workflow(self, call) -> bool:
        """``permissions.manages_call_workflow``, without staff."""
        return (
            self.holds_call_role(call, RoleEnum.CALL_MANAGER)
            or call.manager_id in self.organiser_ids
        )

    def oversees_call(self, call) -> bool:
        """``permissions.oversees_proposal_call``, without staff."""
        return (
            call.pk in self.updatable_call_ids
            or call.manager_id in self.updatable_organisation_ids
        )

    # --- per proposal --------------------------------------------------------

    def load_proposals(self, proposal_ids, call_ids):
        """Load the requested offerings of the proposals and the evaluator
        roles of their calls, two queries for all of them."""
        proposal_ids = set(proposal_ids) - set(self.requested_offering_ids)
        if proposal_ids:
            for proposal_id in proposal_ids:
                self.requested_offering_ids[proposal_id] = set()
            for proposal_id, offering_id in models.RequestedResource.objects.filter(
                proposal_id__in=proposal_ids,
                requested_offering__state=RequestedOfferingStates.ACCEPTED,
            ).values_list("proposal_id", "requested_offering__offering_id"):
                self.requested_offering_ids[proposal_id].add(offering_id)
            self.assigned_roles.update(
                models.ProposalRoleAssignment.objects.filter(
                    proposal_id__in=proposal_ids, revoked_at__isnull=True
                ).values_list("proposal_id", "role_id")
            )
            offering_ids = set().union(
                *(self.requested_offering_ids[pid] for pid in proposal_ids)
            )
            self.staffed_offering_ids.update(
                permissions_models.UserRole.objects.filter(
                    is_active=True,
                    user__is_active=True,
                    role__name=RoleEnum.OFFERING_MANAGER,
                    content_type=ContentType.objects.get_for_model(
                        marketplace_models.Offering
                    ),
                    object_id__in=offering_ids,
                ).values_list("object_id", flat=True)
            )
        call_ids = set(call_ids) - set(self.roles_by_call)
        if call_ids:
            for call_id in call_ids:
                self.roles_by_call[call_id] = []
                self.held_call_roles[call_id] = set()
            for role in models.CallProposalRole.objects.filter(call_id__in=call_ids):
                self.roles_by_call[role.call_id].append(role)
            for call_id, name, template in permissions_models.UserRole.objects.filter(
                is_active=True,
                content_type=ContentType.objects.get_for_model(models.Call),
                object_id__in=call_ids,
            ).values_list("object_id", "role__name", "role__template__name"):
                self.held_call_roles[call_id].update(filter(None, (name, template)))

    def on_team(self, proposal) -> bool:
        return proposal.pk in self.team_proposal_ids

    def holds_call_role_on(self, proposal, call, role_name) -> bool:
        """``permissions.holds_call_role_on_proposal``, but for a reviewer's
        work on the proposal, which the caller checks."""
        if not self.holds_call_role(call, role_name):
            return False
        return (
            role_name == RoleEnum.CALL_MANAGER
            or proposal.pk not in self.conflicted_proposal_ids
        )

    def chairs_panel_for(self, proposal, call) -> bool:
        """``permissions.chairs_panel_for``, but for the applicant team,
        which the caller checks."""
        return self.user.pk in (
            call.panel_chair_id,
            call.panel_vice_chair_id,
        ) and self.holds_call_role_on(proposal, call, RoleEnum.CALL_PANEL_MEMBER)

    def acts_for_applicant(self, proposal) -> bool:
        return proposal.pk in self.managed_proposal_ids

    def manages_requested_offering(self, proposal) -> bool:
        return bool(
            self.requested_offering_ids.get(proposal.pk, set())
            & self.managed_offering_ids
        )

    def resolve_role_id(self, call, role_ref):
        """``proposal_roles.resolve_role`` over the call's loaded roles."""
        ref = str(role_ref or "")
        if ref.startswith(proposal_roles.ROLE_REF_PREFIX):
            ref = ref[len(proposal_roles.ROLE_REF_PREFIX) :]
        roles = self.roles_by_call.get(call.pk, [])
        if ref.isdigit():
            return next((r.pk for r in roles if r.pk == int(ref)), None)
        compact = ref.replace("-", "")
        for role in roles:
            if role.uuid.hex == compact:
                return role.pk
        return next((r.pk for r in roles if r.key == ref), None)

    def holds_role(self, proposal, role_id, lead_only=False) -> bool:
        held = self.led_roles if lead_only else self.held_roles
        return role_id is not None and (proposal.pk, role_id) in held

    def role_is_assigned(self, proposal, role_id) -> bool:
        """Whether anyone holds the role on the proposal, confirmed or not."""
        return (proposal.pk, role_id) in self.assigned_roles

    def role(self, call, role_id):
        return next(
            (r for r in self.roles_by_call.get(call.pk, []) if r.pk == role_id), None
        )


# =============================================================================
# Step rules, evaluated against the standing
# =============================================================================


class StepWork:
    """One proposal step and the user's part in it."""

    def __init__(self, standing, instance, slots):
        self.standing = standing
        self.user = standing.user
        self.instance = instance
        self.call_step = instance.call_step
        self.proposal = instance.proposal
        self.call = self.proposal.round.call
        self.slots = slots

    def manages_proposal_workflow(self) -> bool:
        """``permissions.manages_proposal_workflow``, without staff: the
        call's managers, never on a proposal they apply with."""
        return self.standing.manages_call_workflow(
            self.call
        ) and not self.standing.on_team(self.proposal)

    def holds_step_role(self) -> bool:
        """``permissions.user_holds_step_role``.

        A call reviewer holds a reviewer-completed step only where they
        hold work on it, as the proposal's own reviewers.
        """
        standing, proposal, call_step = self.standing, self.proposal, self.call_step
        role = call_step.completer
        if role == ResponsibleRoles.APPLICANT:
            return standing.acts_for_applicant(proposal)
        if standing.on_team(proposal):
            return False
        if role == ResponsibleRoles.OFFERING_MANAGER:
            return standing.manages_requested_offering(proposal)
        if role == ResponsibleRoles.PROPOSAL_ROLE:
            return standing.holds_role(proposal, call_step.completer_proposal_role_id)
        if role == ResponsibleRoles.PANEL_CHAIR:
            return standing.chairs_panel_for(proposal, self.call)
        role_name = proposal_permissions.RESPONSIBLE_ROLE_TO_CALL_ROLE.get(role)
        if not role_name or not standing.holds_call_role_on(
            proposal, self.call, role_name
        ):
            return False
        if role == ResponsibleRoles.REVIEWER:
            return any(
                slot.contributor_id == self.user.pk
                and slot.status not in Statuses.CLOSED
                for slot in self.slots
            )
        return True

    def completes_step_role(self) -> bool:
        """``permissions.user_completes_step``: the step's completer role,
        held by its lead alone on a lead-only step its contributing role
        completes."""
        call_step = self.call_step
        if call_step.contributors_lead_only and self._completer_role_contributes():
            if self.standing.on_team(self.proposal):
                return False
            return self.standing.holds_role(
                self.proposal, call_step.completer_proposal_role_id, lead_only=True
            )
        return self.holds_step_role()

    def completes(self) -> bool:
        """``permissions.user_can_act_on_active_step``, without staff: the
        call's managers complete every step but the applicant's."""
        if self.call_step.completer == ResponsibleRoles.APPLICANT:
            return self.standing.acts_for_applicant(self.proposal)
        if (
            self.call_step.completer == ResponsibleRoles.CALL_MANAGER
            and self.manages_proposal_workflow()
        ):
            return True
        return self.completes_step_role()

    def may_contribute(self) -> bool:
        """``contributions.may_contribute``."""
        standing, proposal, call_step = self.standing, self.proposal, self.call_step
        if call_step.kind != StepKinds.APPLICANT_ACTION and standing.on_team(proposal):
            return False
        try:
            source, parameter = parse_contributor_source(call_step.contributor_source)
        except ValueError:
            return False
        if source == ContributorSources.NONE:
            return self.holds_step_role()
        if source == ContributorSources.REQUESTED_OFFERING_TEAM:
            return standing.manages_requested_offering(proposal) or (
                call_step.per_requested_offering
                and any(
                    slot.requested_offering_id
                    and slot.requested_offering.offering_id
                    in standing.managed_offering_ids
                    for slot in self.slots
                )
            )
        if source == ContributorSources.ASSIGNED_REVIEWERS:
            return (
                any(
                    slot.contributor_id == self.user.pk
                    and slot.status not in StepContributionStatuses.CLOSED
                    for slot in self.slots
                )
                and proposal.pk not in standing.conflicted_proposal_ids
            ) or self.holds_step_role()
        if source == ContributorSources.CALL_ROLE:
            if parameter == ResponsibleRoles.PANEL_CHAIR:
                return standing.chairs_panel_for(proposal, self.call)
            role_name = proposal_permissions.RESPONSIBLE_ROLE_TO_CALL_ROLE.get(
                parameter
            )
            return bool(role_name) and standing.holds_call_role_on(
                proposal, self.call, role_name
            )
        if source == ContributorSources.PROPOSAL_ROLE:
            # ``contributions.step_role_contributors``: on a lead-only step,
            # the role's lead alone.
            return standing.holds_role(
                proposal,
                standing.resolve_role_id(self.call, call_step.contributor_source),
                lead_only=call_step.contributors_lead_only,
            )
        return False

    def fillable_open_slots(self):
        """The open slots the user fills (``Access.can_fill`` for a
        contributor). A slot of a review is the review's task."""
        standing = self.standing
        on_team = (
            self.call_step.kind != StepKinds.APPLICANT_ACTION
            and standing.on_team(self.proposal)
        )
        if on_team:
            return []
        result = []
        for slot in self.slots:
            if slot.status not in Statuses.OPEN or slot.review_id:
                continue
            if self.delegate_of(slot) is not None:
                # Handed to someone: theirs to fill (``delegated_slots``).
                continue
            if slot.requested_offering_id:
                if slot.requested_offering.offering_id in standing.managed_offering_ids:
                    result.append(slot)
            elif slot.contributor_id == self.user.pk:
                result.append(slot)
        return result

    def delegate_of(self, slot):
        """``contributions.effective_delegate_id`` of ``slot``, asked only of
        the slots handed to someone."""
        if slot.delegate_id is None:
            return None
        if not hasattr(slot, "_effective_delegate_id"):
            slot.proposal_step = self.instance
            slot._effective_delegate_id = contributions.effective_delegate_id(slot)
        return slot._effective_delegate_id

    def delegated_slots(self):
        """The open slots handed to the user that they may take over."""
        return [
            slot
            for slot in self.slots
            if slot.status in Statuses.OPEN
            and slot.delegate_id == self.user.pk
            and self.delegate_of(slot) == self.user.pk
        ]

    def opens_own_slot(self) -> bool:
        """Whether the user's slot is made the first time they open the
        step (``contributions._may_open_own_slot``) and they have none yet.
        A step without contributors is its completer's task, and a reviewer
        gets a slot only with a review."""
        source = self.call_step.contributor_source_kind
        if self.call_step.per_requested_offering or source in (
            ContributorSources.NONE,
            ContributorSources.ASSIGNED_REVIEWERS,
        ):
            return False
        if any(
            slot.contributor_id == self.user.pk and not slot.requested_offering_id
            for slot in self.slots
        ):
            return False
        return True

    def unheld_completer_role(self) -> str:
        """The completer role of the step when nobody can hold it, so the
        call's managers complete the step in its place; empty otherwise.

        A call role (panel member, reviewer) nobody holds on the call, or the
        offering managers of a proposal none of whose offerings has a team.
        """
        completer = self.call_step.completer
        if completer == ResponsibleRoles.PANEL_CHAIR:
            seated = self.call.panel_chair_id or self.call.panel_vice_chair_id
            return "" if seated else completer
        role_name = proposal_permissions.RESPONSIBLE_ROLE_TO_CALL_ROLE.get(completer)
        if completer != ResponsibleRoles.CALL_MANAGER and role_name:
            held = self.standing.held_call_roles.get(self.call.pk, set())
            return "" if role_name in held else completer
        if completer == ResponsibleRoles.OFFERING_MANAGER:
            offerings = self.standing.requested_offering_ids.get(
                self.proposal.pk, set()
            )
            return "" if offerings & self.standing.staffed_offering_ids else completer
        return ""

    def stand_in_slots(self):
        """Open per-offering slots of offerings without a team, which the
        call's managers fill in the team's place."""
        if not self.manages_proposal_workflow():
            return []
        return [
            slot
            for slot in self.slots
            if slot.requested_offering_id
            and slot.status in Statuses.OPEN
            and not slot.review_id
            and slot.requested_offering.offering_id
            not in self.standing.staffed_offering_ids
        ]

    def reviews_short(self) -> str:
        """The reviews a step taking them from assigned reviewers still
        lacks while no reviewer is working on the proposal; empty when it
        lacks none or reviews are under way."""
        call_step = self.call_step
        needed = call_step.min_contributions or 0
        if (
            call_step.contributor_source_kind != ContributorSources.ASSIGNED_REVIEWERS
            or not needed
            or any(slot.status in Statuses.OPEN for slot in self.slots)
        ):
            return ""
        submitted = sum(
            1
            for slot in self.slots
            if slot.review_id and slot.status == Statuses.SUBMITTED
        )
        if submitted >= needed:
            return ""
        return (
            f"{call_step.name} needs {needed} reviews; {submitted} "
            f"{'is' if submitted == 1 else 'are'} in and no reviewer is working "
            "on it: assign reviewers"
        )

    def completion_need(self):
        kind = self.call_step.kind
        if kind == StepKinds.APPLICANT_ACTION:
            return Needs.RESPOND
        if kind == StepKinds.ASSIGNMENT:
            return Needs.ASSIGN
        if kind == StepKinds.DECISION:
            return Needs.DECIDE
        return Needs.COMPLETE

    def completion_section(self):
        call_step = self.call_step
        if call_step.kind == StepKinds.ASSIGNMENT:
            return EVALUATORS_SECTION
        # A review is shown as a review, not on a contribution card: a step
        # of reviews is completed from its actions.
        # Its contribution cards, when it has any: with no slot yet -- its
        # evaluator role still unheld, say -- the page draws no such section.
        if (
            call_step.collects_contributions
            and call_step.contributor_source_kind
            != ContributorSources.ASSIGNED_REVIEWERS
            and self.slots
        ):
            return contributions_section(call_step)
        if call_step.checklist_id:
            return CHECKLIST_SECTION
        return STEP_ACTIONS_SECTION

    def tasks(self):
        """The tasks the step holds for the user: their own work on it, and
        for the call's managers the evaluators it still lacks."""
        return self._work_tasks() + self._assignment_tasks()

    def unheld_role(self):
        """The evaluator role the step waits on that nobody holds on the
        proposal: its contributors' or its completer's. Nobody else is told,
        so the proposal would stand at the step unnoticed. An assignment
        step is about assigning, and is its completer's task already."""
        call_step = self.call_step
        if call_step.kind == StepKinds.ASSIGNMENT:
            return None
        refs = []
        try:
            source, parameter = parse_contributor_source(call_step.contributor_source)
        except ValueError:
            source = None
        if source == ContributorSources.PROPOSAL_ROLE:
            refs.append(parameter)
        if (
            call_step.completer == ResponsibleRoles.PROPOSAL_ROLE
            and call_step.completer_proposal_role_id
        ):
            refs.append(call_step.completer_proposal_role_id)
        for ref in refs:
            role_id = self.standing.resolve_role_id(self.call, ref)
            if role_id is not None and not self.standing.role_is_assigned(
                self.proposal, role_id
            ):
                return self.standing.role(self.call, role_id)
        return None

    def _assignment_tasks(self):
        if not self.manages_proposal_workflow():
            return []
        role = self.unheld_role()
        if role is None:
            return []
        return [
            self._task(
                Needs.ASSIGN,
                key=f"step:{self.instance.uuid.hex}:assign:{role.uuid.hex}",
                section=EVALUATORS_SECTION,
                detail=role.name,
            )
        ]

    def _work_tasks(self):
        """The user's open contributions, else completing the step."""
        if not self.may_contribute():
            own = []
        else:
            own = [
                self._task(
                    Needs.CONTRIBUTE,
                    key=f"contribution:{slot.uuid.hex}",
                    offering=self._slot_offering(slot),
                    due_at=slot.due_at or self.instance.deadline,
                    section=contributions_section(self.call_step),
                    detail=(
                        slot.requested_offering.offering.name
                        if slot.requested_offering_id
                        else ""
                    ),
                )
                for slot in self.fillable_open_slots()
            ]
            if not own and self.opens_own_slot():
                own.append(
                    self._task(
                        Needs.CONTRIBUTE,
                        key=f"step:{self.instance.uuid.hex}:contribute",
                        section=contributions_section(self.call_step),
                    )
                )
        own.extend(
            self._task(
                (
                    Needs.ACCEPT_DELEGATION
                    if slot.status == Statuses.PENDING
                    else Needs.CONTRIBUTE
                ),
                key=f"contribution:{slot.uuid.hex}",
                offering=self._slot_offering(slot),
                due_at=slot.due_at or self.instance.deadline,
                section=contributions_section(self.call_step),
                detail=(
                    slot.requested_offering.offering.name
                    if slot.requested_offering_id
                    else ""
                ),
                coi_declaration_text=(
                    coi_declarations.for_contribution(slot)
                    if slot.status == Statuses.PENDING
                    else ""
                ),
            )
            for slot in self.delegated_slots()
            if not any(task.key == f"contribution:{slot.uuid.hex}" for task in own)
        )
        own.extend(
            self._task(
                Needs.CONTRIBUTE,
                key=f"contribution:{slot.uuid.hex}",
                offering=self._slot_offering(slot),
                due_at=slot.due_at or self.instance.deadline,
                section=contributions_section(self.call_step),
                detail=STAND_IN_SLOT.format(
                    offering=slot.requested_offering.offering.name
                ),
            )
            for slot in self.stand_in_slots()
            if not any(task.key == f"contribution:{slot.uuid.hex}" for task in own)
        )
        if own:
            return own
        short = self.reviews_short()
        if short:
            # The step cannot complete before more reviewers are assigned:
            # that, not completing it, is the call manager's task.
            if not self.manages_proposal_workflow():
                return []
            return [
                self._task(
                    Needs.ASSIGN,
                    key=f"step:{self.instance.uuid.hex}:reviewers",
                    detail=short,
                )
            ]
        detail = ""
        if not self.completes():
            unheld = self.unheld_completer_role()
            if not unheld or not self.manages_proposal_workflow():
                return []
            detail = STAND_IN_COMPLETER.format(
                role=dict(ResponsibleRoles.CHOICES).get(unheld, unheld).lower()
            )
        if not self._completion_is_due():
            return []
        need = self.completion_need()
        section = self.completion_section()
        if need == Needs.RESPOND and self.call_step.key == AWARD_RESPONSE_KEY:
            need, section = Needs.ACCEPT_AWARD, ALLOCATION_SECTION
        elif need == Needs.RESPOND:
            section = response_section(self.call_step)
        return [
            self._task(
                need,
                key=f"step:{self.instance.uuid.hex}",
                section=section,
                detail=detail,
            )
        ]

    def _completion_is_due(self) -> bool:
        """A completer whose role is the contributors' own completes once the
        work is in: until then the step is waiting on contributions, not on
        them. A completer overseeing others' work is always on the hook."""
        if not self.call_step.collects_contributions:
            return True
        if not (
            contributions._completer_is_a_contributor(self.call_step)
            or self._completer_role_contributes()
        ):
            return True
        return not any(slot.status in Statuses.OPEN for slot in self.slots)

    def _completer_role_contributes(self) -> bool:
        """The holders of one evaluator role contribute to the step and
        complete it -- rapporteurs writing their report, say: one of them
        completes once the others' work is in too."""
        call_step = self.call_step
        if call_step.completer != ResponsibleRoles.PROPOSAL_ROLE:
            return False
        try:
            source, parameter = parse_contributor_source(call_step.contributor_source)
        except ValueError:
            return False
        if source != ContributorSources.PROPOSAL_ROLE:
            return False
        role_id = self.standing.resolve_role_id(self.call, parameter)
        return role_id is not None and role_id == call_step.completer_proposal_role_id

    def _slot_offering(self, slot):
        if not slot.requested_offering_id:
            return None
        offering = slot.requested_offering.offering
        return self.standing._offerings.setdefault(offering.pk, offering)

    def _task(
        self,
        need,
        key,
        due_at=None,
        section=None,
        detail="",
        coi_declaration_text="",
        offering=None,
    ):
        if offering is None and (
            self.call_step.completer == ResponsibleRoles.OFFERING_MANAGER
            or self.call_step.contributor_source
            == ContributorSources.REQUESTED_OFFERING_TEAM
        ):
            offering = self.standing.managed_requested_offering(self.proposal)
        return Task(
            key=key,
            needed=need,
            target=Targets.PROPOSAL,
            proposal=self.proposal,
            call=self.call,
            call_round=self.proposal.round,
            instance=self.instance,
            due_at=due_at or self.instance.deadline,
            section=section,
            detail=detail,
            coi_declaration_text=coi_declaration_text,
            offering=offering,
        )


# =============================================================================
# The list
# =============================================================================


def _step_candidates(standing, statuses):
    """Step instances where the user could hold work: a superset, narrowed
    by the rules above."""
    user = standing.user
    call_ids = list(standing.call_roles)
    reach = (
        Q(proposal__round__call_id__in=call_ids)
        | Q(proposal__round__call__manager_id__in=standing.organiser_ids)
        | Q(proposal__round__call__manager_id__in=standing.updatable_organisation_ids)
        | Q(proposal_id__in=standing.held_role_proposal_ids)
        | Q(
            proposal_id__in=standing.managed_proposal_ids,
            call_step__kind=StepKinds.APPLICANT_ACTION,
        )
        | Q(contributions__contributor=user)
        | Q(contributions__delegate=user)
    )
    if standing.managed_offering_ids:
        reach |= Q(
            Exists(
                models.RequestedResource.objects.filter(
                    proposal_id=OuterRef("proposal_id"),
                    requested_offering__state=RequestedOfferingStates.ACCEPTED,
                    requested_offering__offering_id__in=standing.managed_offering_ids,
                )
            )
        )
    return (
        models.ProposalWorkflowStepInstance.objects.filter(status__in=statuses)
        .filter(reach)
        .select_related(
            "call_step",
            "proposal__round__call",
        )
        .distinct()
    )


def _slots_by_instance(instance_ids):
    slots = {}
    for slot in models.StepContribution.objects.filter(
        proposal_step_id__in=instance_ids
    ).select_related("requested_offering__offering__customer", "delegate"):
        slots.setdefault(slot.proposal_step_id, []).append(slot)
    return slots


def _active_step_tasks(standing):
    # A step run once per round is the round's work, listed per round below.
    instances = list(
        _step_candidates(standing, [WorkflowStepInstanceStatuses.ACTIVE])
        .filter(
            # In evaluation, or in an accepted proposal's open post-award run.
            Q(proposal__state__in=OPEN_PROPOSAL_STATES)
            | Q(run__state=ProposalRunStates.OPEN)
        )
        .exclude(call_step__round_scoped=True)
    )
    if not instances:
        return []
    standing.load_proposals(
        [i.proposal_id for i in instances],
        [i.proposal.round.call_id for i in instances],
    )
    slots = _slots_by_instance([i.pk for i in instances])
    tasks = []
    for instance in instances:
        work = StepWork(standing, instance, slots.get(instance.pk, []))
        tasks.extend(work.tasks())
    return tasks


def _release_tasks(standing):
    """Completed assessments whose report waits to be released to the
    applicant (``step_inputs.can_release``)."""
    instances = list(
        _step_candidates(standing, [WorkflowStepInstanceStatuses.COMPLETED])
        .filter(
            call_step__kind=StepKinds.ASSESSMENT,
            call_step__release_to_applicant=True,
            released_at__isnull=True,
        )
        .exclude(proposal__state__in=(ProposalStates.DRAFT, ProposalStates.CANCELED))
    )
    if not instances:
        return []
    standing.load_proposals(
        [i.proposal_id for i in instances],
        [i.proposal.round.call_id for i in instances],
    )
    slots = _slots_by_instance([i.pk for i in instances])
    tasks = []
    for instance in instances:
        proposal = instance.proposal
        if standing.on_team(proposal):
            continue
        work = StepWork(standing, instance, slots.get(instance.pk, []))
        if not standing.oversees_call(work.call) and (
            instance.call_step.completer == ResponsibleRoles.APPLICANT
            or not work.completes_step_role()
        ):
            continue
        tasks.append(
            Task(
                key=f"release:{instance.uuid.hex}",
                needed=Needs.RELEASE_REPORT,
                target=Targets.PROPOSAL,
                proposal=proposal,
                call=work.call,
                call_round=proposal.round,
                instance=instance,
                section=RELEASED_REPORTS_SECTION,
            )
        )
    return tasks


def _advance_tasks(standing):
    """Workflows parked on a completed step of manual transition, for the
    call's managers (``workflow_service.is_awaiting_manual_advance``)."""
    call_ids = [
        call_id
        for call_id, roles in standing.call_roles.items()
        if RoleEnum.CALL_MANAGER in roles
    ]
    proposals = (
        models.Proposal.objects.filter(
            Q(round__call_id__in=call_ids)
            | Q(round__call__manager_id__in=standing.organiser_ids),
            state__in=OPEN_PROPOSAL_STATES,
            decision_held=False,
            current_step__status=WorkflowStepInstanceStatuses.COMPLETED,
            current_step__call_step__transition_mode=TransitionModes.MANUAL,
        )
        .exclude(pk__in=standing.team_proposal_ids)
        .select_related("round__call", "current_step__call_step")
    )
    return [
        Task(
            key=f"advance:{proposal.uuid.hex}",
            needed=Needs.ADVANCE,
            target=Targets.PROPOSAL,
            proposal=proposal,
            call=proposal.round.call,
            call_round=proposal.round,
            instance=proposal.current_step,
        )
        for proposal in proposals
    ]


def _round_report_tasks(standing):
    """Draft round reports of the user's, of a round step opened and still
    covering proposals."""
    reports = (
        models.RoundMeetingNote.objects.filter(
            author=standing.user,
            call_step__round_scoped=True,
            submitted_at__isnull=True,
            proposals__state=ProposalStates.IN_REVIEW,
        )
        .select_related("round__call", "call_step")
        .distinct()
    )
    tasks = []
    for report in reports:
        round_step = models.RoundStep.objects.filter(
            round=report.round, call_step=report.call_step
        ).first()
        if round_step is None or not round_step.is_open:
            continue
        tasks.append(
            Task(
                key=f"round_report:{report.uuid.hex}",
                needed=Needs.WRITE_ROUND_REPORT,
                target=Targets.ROUND_REPORT,
                call=report.round.call,
                call_round=report.round,
                due_at=round_step.due_at,
                target_uuid=report.uuid.hex,
                detail=report.call_step.name or report.call_step.key,
            )
        )
    return tasks


def _round_step_tasks(standing):
    """Round steps with proposals standing at them, for the call's managers:
    to open while not opened, and to complete for the round once opened when
    nobody writes reports for them."""
    call_ids = [
        call_id
        for call_id, roles in standing.call_roles.items()
        if RoleEnum.CALL_MANAGER in roles
    ]
    rows = (
        models.ProposalWorkflowStepInstance.objects.filter(
            Q(proposal__round__call_id__in=call_ids)
            | Q(proposal__round__call__manager_id__in=standing.organiser_ids),
            status=WorkflowStepInstanceStatuses.ACTIVE,
            call_step__round_scoped=True,
            proposal__state=ProposalStates.IN_REVIEW,
        )
        .values_list("proposal__round_id", "call_step_id")
        .distinct()
    )
    tasks = []
    for round_id, call_step_id in rows:
        call_step = models.CallWorkflowStep.objects.select_related("call").get(
            pk=call_step_id
        )
        call_round = models.Round.objects.get(pk=round_id)
        round_step = models.RoundStep.objects.filter(
            round_id=round_id, call_step_id=call_step_id
        ).first()
        if round_step is None or not round_step.is_open:
            needed = Needs.OPEN_ROUND_STEP
        elif not call_step.collects_contributions:
            needed = Needs.COMPLETE_ROUND_STEP
        else:
            continue
        tasks.append(
            Task(
                key=f"{needed}:{call_round.uuid.hex}:{call_step.uuid.hex}",
                needed=needed,
                target=Targets.ROUND,
                call=call_step.call,
                call_round=call_round,
                due_at=round_step.due_at if round_step else None,
                target_uuid=call_round.uuid.hex,
                detail=call_step.name or call_step.key,
            )
        )
    return tasks


def _change_request_tasks(standing):
    """Changes asked of the applicant and not yet submitted, for the
    proposal's managers, who submit them (``change_requests.
    open_applicant_requests``). One task per proposal however many steps
    asked, as one submission answers them all: due when the earliest of
    them is, and named after that request's step. Gone once they close."""
    if not standing.managed_proposal_ids:
        return []
    requests = (
        change_requests.open_requests()
        .filter(
            proposal_id__in=standing.managed_proposal_ids,
            proposal__state=ProposalStates.IN_REVIEW,
            contribution__isnull=True,
            step_instance__status=WorkflowStepInstanceStatuses.ACTIVE,
        )
        .select_related("proposal__round__call", "step_instance__call_step")
        .order_by("created", "id")
    )
    first = {}
    for change_request in requests:
        current = first.get(change_request.proposal_id)
        if current is None or (
            change_request.due_at is not None
            and (current.due_at is None or change_request.due_at < current.due_at)
        ):
            first[change_request.proposal_id] = change_request
    return [
        Task(
            key=f"changes:{change_request.proposal.uuid.hex}",
            needed=Needs.SUBMIT_CHANGES,
            target=Targets.PROPOSAL,
            proposal=change_request.proposal,
            call=change_request.proposal.round.call,
            call_round=change_request.proposal.round,
            instance=change_request.step_instance,
            due_at=change_request.due_at,
        )
        for change_request in first.values()
    ]


def _review_tasks(standing):
    """Reviews the user is writing, on proposals still being evaluated and
    with no conflict of interest standing in the way."""
    user = standing.user
    reviews = (
        models.Review.objects.filter(
            reviewer=user,
            state=models.Review.States.IN_REVIEW,
            proposal__state__in=OPEN_PROPOSAL_STATES,
        )
        .exclude(proposal_id__in=standing.team_proposal_ids)
        .exclude(
            Exists(
                models.ConflictOfInterest.objects.filter(
                    reviewer__user_id=user.pk,
                    proposal_id=OuterRef("proposal_id"),
                    status__in=proposal_roles.BLOCKING_COI_STATUSES,
                )
            )
        )
        .select_related(
            "proposal__round__call",
            "assignment_item__batch",
            "contribution__proposal_step__call_step",
        )
    )
    tasks = []
    for review in reviews:
        proposal = review.proposal
        try:
            contribution = review.contribution
        except models.StepContribution.DoesNotExist:
            contribution = None
        instance = contribution.proposal_step if contribution else None
        tasks.append(
            Task(
                key=f"review:{review.uuid.hex}",
                needed=Needs.REVIEW,
                target=Targets.REVIEW,
                target_uuid=review.uuid.hex,
                proposal=proposal,
                call=proposal.round.call,
                call_round=proposal.round,
                instance=instance,
                due_at=(contribution and contribution.due_at) or review.review_end_date,
            )
        )
    return tasks


def _assignment_tasks(standing):
    """Proposals offered to the user in a batch still awaiting an answer.
    The title is all an unanswered assignment discloses."""
    now = timezone.now()
    items = (
        models.AssignmentItem.objects.filter(
            batch__reviewer_pool_entry__reviewer__user=standing.user,
            batch__status=AssignmentBatchStatuses.SENT,
            status=AssignmentItemStatuses.PENDING,
        )
        .filter(Q(batch__expires_at__isnull=True) | Q(batch__expires_at__gt=now))
        .select_related("batch__call", "proposal__round")
    )
    return [
        Task(
            key=f"assignment:{item.uuid.hex}",
            needed=Needs.ANSWER_ASSIGNMENT,
            target=Targets.ASSIGNMENTS,
            target_uuid=item.batch.uuid.hex,
            proposal=item.proposal,
            call=item.batch.call,
            call_round=item.proposal.round,
            due_at=item.batch.expires_at,
        )
        for item in items
    ]


def _invitation_tasks(standing):
    """Invitations to a call's reviewer pool the user has yet to answer."""
    user = standing.user
    now = timezone.now()
    addressed = Q(reviewer__user=user) | Q(invited_user=user)
    if user.email:
        addressed |= Q(invited_email=user.email)
    entries = (
        models.CallReviewerPool.objects.filter(addressed)
        .filter(invitation_status=ReviewerPoolInvitationStatuses.PENDING)
        .filter(
            Q(invitation_expires_at__isnull=True) | Q(invitation_expires_at__gt=now)
        )
        .select_related("call")
    )
    return [
        Task(
            key=f"invitation:{entry.uuid.hex}",
            needed=Needs.ANSWER_INVITATION,
            target=Targets.INVITATIONS,
            target_uuid=entry.uuid.hex,
            call=entry.call,
            due_at=entry.invitation_expires_at,
        )
        for entry in entries
    ]


def _coi_confirmation_tasks(standing):
    """Evaluator roles waiting for the holder to confirm having no conflict
    of interest, before they open the proposal."""
    assignments = (
        proposal_roles.with_standing_conflict(
            models.ProposalRoleAssignment.objects.filter(
                user=standing.user,
                revoked_at__isnull=True,
                coi_confirmation_required=True,
                coi_confirmed_at__isnull=True,
                proposal__state__in=OPEN_PROPOSAL_STATES,
            )
        )
        .filter(has_standing_conflict=False)
        .exclude(proposal_id__in=standing.team_proposal_ids)
        .select_related(
            "proposal__round__call", "role__call", "assigned_in__call_step__call"
        )
    )
    return [
        Task(
            key=f"evaluator-role:{assignment.uuid.hex}",
            needed=Needs.CONFIRM_NO_COI,
            target=Targets.EVALUATOR_ROLES,
            target_uuid=assignment.uuid.hex,
            proposal=assignment.proposal,
            call=assignment.proposal.round.call,
            call_round=assignment.proposal.round,
            detail=assignment.role.name,
            coi_declaration_text=coi_declarations.for_assignment(assignment),
        )
        for assignment in assignments
    ]


def _offering_request_tasks(standing):
    """Requests to include an offering in a call that the user may answer,
    as ``permissions.user_can_accept_requested_offering`` reads it: the
    permission held on the offering or on its provider organisation."""
    user = standing.user
    requests = (
        models.RequestedOffering.objects.filter(state=RequestedOfferingStates.REQUESTED)
        .filter(
            Q(
                offering_id__in=get_connected_offerings_by_permission(
                    user, PermissionEnum.ACCEPT_REQUESTED_OFFERING
                )
            )
            | Q(
                offering__customer_id__in=get_connected_customers_by_permission(
                    user, PermissionEnum.ACCEPT_REQUESTED_OFFERING
                )
            )
        )
        # An archived call asks nothing more of its providers.
        .exclude(call__state=CallStates.ARCHIVED)
        .select_related("call", "offering__customer")
        .order_by("created", "id")
    )
    return [
        Task(
            key=f"offering-request:{request.uuid.hex}",
            needed=Needs.ANSWER_OFFERING_REQUEST,
            target=Targets.OFFERING_REQUESTS,
            target_uuid=request.offering.customer.uuid.hex,
            call=request.call,
            detail=request.offering.name,
            offering=request.offering,
        )
        for request in requests
    ]


def _sort_key(task):
    far = datetime.datetime.max.replace(tzinfo=datetime.UTC)
    return (task.due_at is None, task.due_at or far, task.key)


def tasks_of(user, offering_uuid=None, provider_uuid=None):
    """Every open task of ``user``, the soonest due first; with
    ``offering_uuid`` or ``provider_uuid``, only the tasks about that offering
    or about an offering of that provider."""
    if user is None or user.is_anonymous:
        return []
    standing = Standing(user)
    tasks = [
        *_active_step_tasks(standing),
        *_release_tasks(standing),
        *_advance_tasks(standing),
        *_round_report_tasks(standing),
        *_round_step_tasks(standing),
        *_change_request_tasks(standing),
        *_review_tasks(standing),
        *_assignment_tasks(standing),
        *_invitation_tasks(standing),
        *_coi_confirmation_tasks(standing),
        *_offering_request_tasks(standing),
    ]
    if offering_uuid:
        tasks = [
            t for t in tasks if t.offering and t.offering.uuid.hex == offering_uuid
        ]
    if provider_uuid:
        tasks = [
            t
            for t in tasks
            if t.offering
            and t.offering.customer_id
            and t.offering.customer.uuid.hex == provider_uuid
        ]
    _set_views(standing, tasks)
    return sorted(tasks, key=_sort_key)


class Due:
    """How close a task is to its deadline, as the list can be narrowed to.

    Disjoint: a task past its deadline is overdue, not also due soon, so the
    counts of the two add up to the tasks that need attention first."""

    OVERDUE = "overdue"
    SOON = "due_soon"
    CHOICES = (
        (OVERDUE, "Overdue"),
        (SOON, "Due soon"),
    )


def due_soon_days():
    """How many days ahead a deadline counts as due soon."""
    return config.PROPOSAL_DASHBOARD_REVIEWS_DUE_WITHIN_DAYS


def _due_of(task, now, horizon):
    if task.due_at is None:
        return None
    if task.due_at < now:
        return Due.OVERDUE
    if task.due_at <= horizon:
        return Due.SOON
    return None


def with_due(tasks, due):
    """The tasks whose deadline is ``due`` (one of ``Due``)."""
    now = timezone.now()
    horizon = now + datetime.timedelta(days=due_soon_days())
    return [task for task in tasks if _due_of(task, now, horizon) == due]


def summary_of(tasks):
    """How many tasks there are, and how many are overdue or due soon: the
    counts ``with_due`` narrows the list to, from one pass over it."""
    days = due_soon_days()
    now = timezone.now()
    horizon = now + datetime.timedelta(days=days)
    dues = [_due_of(task, now, horizon) for task in tasks]
    return {
        "total": len(tasks),
        "overdue": dues.count(Due.OVERDUE),
        "due_soon": dues.count(Due.SOON),
        "due_soon_days": days,
    }


def _set_views(standing, tasks):
    """Each task's view, with the organisations managing the calls of the
    call team's tasks loaded in one query."""
    for task in tasks:
        task.view, task.view_customer = _view_of(standing, task)
    manager_ids = {task.view_customer for task in tasks if task.view == Views.CALL_TEAM}
    customers = dict(
        models.CallManagingOrganisation.objects.filter(pk__in=manager_ids).values_list(
            "pk", "customer__uuid"
        )
    )
    for task in tasks:
        if task.view == Views.CALL_TEAM:
            task.view_customer = customers.get(task.view_customer)


def _view_of(standing, task):
    """Whose view of the proposal page opens ``task``, by the standing that
    gave the user the task: the applicant team's own, the call's managers'
    (organisers among them), the offering team's, else the plain page."""
    if task.target != Targets.PROPOSAL or task.proposal is None:
        return None, None
    if standing.on_team(task.proposal):
        return Views.APPLICANT, None
    call = task.call or task.proposal.round.call
    if standing.manages_call_workflow(call) or standing.oversees_call(call):
        # The organisation managing the call, by id: ``_set_views`` loads
        # whose it is for all the tasks at once.
        return Views.CALL_TEAM, call.manager_id
    offering = task.offering
    if offering is not None and offering.pk in standing.offering_team_ids:
        # A slot handed to someone holding a role on the offering opens in
        # the offering team's view too, as the managers' do.
        return Views.OFFERING_TEAM, offering.customer.uuid
    return Views.PARTICIPANT, None
