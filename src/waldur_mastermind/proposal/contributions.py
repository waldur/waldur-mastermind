"""Contributions: the unit of work inside a proposal's workflow step.

A call step says who contributes to it (``contributor_source``) and whether
it has one contribution per requested offering (``per_requested_offering``).
When the step starts, the slots known up front are created: one per
requested offering, or one per holder of an evaluator role. Reviewers get
theirs when they take a review on, and everybody else -- holders of a call
role, the offering managers of a step that is not split per offering, or
whoever completes a step without contributors -- the first time they open
the step's form.

The step's form is answered per contribution, in a checklist completion
scoped to the contribution, so the answers of one contributor, or of one
step, never mix with another's.
"""

from uuid import UUID

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import Avg, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist import utils as checklist_utils
from waldur_core.checklist.enums import QuestionTypes
from waldur_core.core.permissions import has_staff_access
from waldur_core.permissions import models as permissions_models
from waldur_core.permissions import utils as permission_utils
from waldur_core.permissions.enums import RoleEnum
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import (
    change_requests,
    coi_declarations,
    dry_run_context,
    models,
    proposal_roles,
    round_reports,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    SCORE_SPREAD_ABOVE,
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    ChangeRequestParts,
    ChangeRequestResolutions,
    ContributionOutcomes,
    ContributorSources,
    DelegationTargets,
    RequestedOfferingStates,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
    parse_contributor_source,
    parse_delegation_target,
)

Statuses = StepContributionStatuses
User = get_user_model()

# Sources whose contributors are the holders of a role: someone who no
# longer holds it no longer fills its slots.
ROLE_SOURCES = (ContributorSources.PROPOSAL_ROLE, ContributorSources.CALL_ROLE)


class ContributionError(ValueError):
    """The contribution cannot be acted on as asked."""


# =============================================================================
# Who contributes
# =============================================================================


def proposal_role_holders(proposal, role_ref, lead_only=False):
    """Users holding the evaluator role ``role_ref`` on ``proposal``.

    ``role_ref`` is the step's whole contributor source,
    ``proposal_role:<pk or uuid>``. Each holder gets a contribution of their
    own when the step starts, and may open its form. Resolved by the
    evaluator-roles resolver, so contributors are the active holders who
    confirmed they have no conflict of interest. ``lead_only`` keeps the
    role's lead alone.
    """
    return proposal_roles.proposal_role_holders(proposal, role_ref, lead_only=lead_only)


def step_role_contributors(proposal, call_step):
    """The holders of the evaluator role a ``proposal_role`` step names who
    contribute to it: all of them, or the lead alone on a lead-only step."""
    return proposal_role_holders(
        proposal,
        call_step.contributor_source,
        lead_only=call_step.contributors_lead_only,
    )


def conflicted_contributor_ids(proposal) -> set[int]:
    """Ids of the people whose own contributions to the proposal's evaluation
    no longer count: a conflict of interest with it stands -- recorded, or
    recused for -- or they are on its applicant team, a conflict of its own.
    """
    return proposal_roles.users_with_recorded_conflicts(
        proposal
    ) | proposal_roles.applicant_team_user_ids(proposal)


def _excluding_conflicted(contributions, instance):
    """``contributions`` to the step ``instance`` without the per-person ones
    of conflicted people (``conflicted_contributor_ids``). A per-offering
    slot is its offering team's, whoever on it submitted it, and an
    applicant step is the applicant team's own."""
    if instance.call_step.kind == StepKinds.APPLICANT_ACTION:
        return contributions
    conflicted = conflicted_contributor_ids(instance.proposal)
    if not conflicted:
        return contributions
    # A person's slot written by the one it was handed to is theirs as
    # much as its holder's.
    return contributions.exclude(
        requested_offering__isnull=True, contributor_id__in=conflicted
    ).exclude(requested_offering__isnull=True, delegate_id__in=conflicted)


def counted_contributions(instance):
    """The step's submitted contributions that count towards its gates.

    On a lead-only step, those of the role's current lead: a contribution
    submitted by an earlier lead stays on record but no longer counts. Nor
    does one whose contributor has since turned out to be conflicted (see
    ``conflicted_contributor_ids``): it stays on record, but neither counts
    towards the step's gates nor decides its outcome.
    """
    submitted = _excluding_conflicted(
        instance.contributions.filter(status=Statuses.SUBMITTED), instance
    )
    call_step = instance.call_step
    if call_step.contributors_lead_only:
        submitted = submitted.filter(
            contributor__in=step_role_contributors(instance.proposal, call_step)
        )
    return submitted


def step_counts(instance) -> dict:
    """Counts of the work on the step ``instance``, for its dashboard labels
    (``status_labels.PLACEHOLDERS``). Counts only: nobody is named.

    - ``invited``: the people asked to contribute -- every slot the step
      gave out, withdrawn ones aside, and on a step taking reviews from
      assigned reviewers the invitations sent for it not yet taken on;
    - ``accepted``: contributions taken on or submitted;
    - ``declined``: contributions declined, and invitations declined;
    - ``submitted``: contributions counting towards the step's gates
      (``counted_contributions``);
    - ``expected``: the step's minimum of contributions, else the slots
      still open or submitted.
    """
    call_step = instance.call_step
    contributions = instance.contributions.all()
    by_status = {}
    for status in contributions.values_list("status", flat=True):
        by_status[status] = by_status.get(status, 0) + 1
    invited = sum(n for status, n in by_status.items() if status != Statuses.WITHDRAWN)
    declined = by_status.get(Statuses.DECLINED, 0)
    if call_step.contributor_source_kind == ContributorSources.ASSIGNED_REVIEWERS:
        items = (
            models.AssignmentItem.objects.filter(
                proposal_id=instance.proposal_id,
                batch__status__in=AssignmentBatchStatuses.SENT_TO_REVIEWER,
                review__isnull=True,
            )
            .filter(Q(batch__call_step=call_step) | Q(batch__call_step__isnull=True))
            .values_list("status", flat=True)
        )
        for status in items:
            if status in (
                AssignmentItemStatuses.PENDING,
                AssignmentItemStatuses.DECLINED,
                AssignmentItemStatuses.EXPIRED,
            ):
                invited += 1
            if status == AssignmentItemStatuses.DECLINED:
                declined += 1
    submitted = counted_contributions(instance).count()
    expected = call_step.min_contributions or sum(
        by_status.get(status, 0) for status in (*Statuses.OPEN, Statuses.SUBMITTED)
    )
    return {
        "invited": invited,
        "accepted": by_status.get(Statuses.ACCEPTED, 0)
        + by_status.get(Statuses.SUBMITTED, 0),
        "declined": declined,
        "submitted": submitted,
        "expected": expected,
        "reports_submitted": submitted,
        "reports_expected": expected,
    }


def requested_offerings_of(proposal, call_step=None):
    """The accepted offerings the proposal requests resources from; on a
    step of an extension or closing, the call offerings its award
    provisioned resources on."""
    if call_step is not None and call_step.is_post_award:
        # Not one whose order the provider refused: it provisioned nothing
        # there to extend or report on.
        ids = (
            models.AwardedResource.objects.filter(
                proposal=proposal, resource__isnull=False
            )
            .annotate(refused=models.awarded_order_refused())
            .filter(refused=False)
            .values_list("requested_offering_id", flat=True)
            .distinct()
        )
    else:
        ids = (
            models.RequestedResource.objects.filter(
                proposal=proposal,
                requested_offering__state=RequestedOfferingStates.ACCEPTED,
            )
            .values_list("requested_offering_id", flat=True)
            .distinct()
        )
    return (
        models.RequestedOffering.objects.filter(id__in=ids)
        .select_related("offering")
        .order_by("id")
    )


def offering_ids_managed_by(user):
    """Ids of the offerings whose team ``user`` is on."""
    return permissions_models.UserRole.objects.filter(
        user=user,
        is_active=True,
        role__name=RoleEnum.OFFERING_MANAGER,
        content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
    ).values_list("object_id", flat=True)


def offering_ids_with_role_of(user):
    """Ids of the offerings ``user`` holds any active role on: those they
    manage, and those whose per-offering slots may be handed to them."""
    return permissions_models.UserRole.objects.filter(
        user=user,
        is_active=True,
        content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
    ).values_list("object_id", flat=True)


def offering_team_providers_by_proposal(proposal_ids, offering_ids) -> dict:
    """Per proposal id, the providers, by name, of those offerings among
    ``offering_ids`` that the proposal requests: whose offering team the
    holder of those roles opens it as. Each as ``{uuid, name}``; one query
    for them all."""
    result = {pk: [] for pk in proposal_ids}
    if not offering_ids or not proposal_ids:
        return result
    rows = (
        models.RequestedResource.objects.filter(
            proposal_id__in=proposal_ids,
            requested_offering__offering_id__in=offering_ids,
            requested_offering__offering__customer__isnull=False,
        )
        .values_list(
            "proposal_id",
            "requested_offering__offering__customer__uuid",
            "requested_offering__offering__customer__name",
        )
        .distinct()
        .order_by("requested_offering__offering__customer__name")
    )
    for proposal_id, uuid, name in rows:
        providers = result.setdefault(proposal_id, [])
        if all(item["uuid"] != uuid.hex for item in providers):
            providers.append({"uuid": uuid.hex, "name": name})
    return result


def offering_team_providers(proposal, offering_ids) -> list[dict]:
    """``offering_team_providers_by_proposal`` of one proposal."""
    return offering_team_providers_by_proposal([proposal.pk], offering_ids)[proposal.pk]


def offering_team_provider_uuid(user, proposal) -> str | None:
    """The provider whose offering team ``user`` opens ``proposal`` as, or
    None: the first by name when they are on several."""
    providers = offering_team_providers(proposal, list(offering_ids_with_role_of(user)))
    return providers[0]["uuid"] if providers else None


def offering_role_holder_ids(offering_id):
    """Ids of the users holding any active role on the offering: its
    managers, and whoever a deployment's own offering roles name -- a
    technical reviewer, a call coordinator. A role that grants no permissions
    opens nothing on the provider side of the marketplace."""
    return permissions_models.UserRole.objects.filter(
        is_active=True,
        user__is_active=True,
        content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
        object_id=offering_id,
    ).values("user_id")


def is_on_offering_team(user, requested_offering) -> bool:
    return (
        offering_ids_managed_by(user)
        .filter(object_id=requested_offering.offering_id)
        .exists()
    )


def oversees_step(user, proposal) -> bool:
    """Staff and the call's managers, who may act on any contribution."""
    return Access(user).oversees(proposal)


def may_contribute(user, instance) -> bool:
    """Whether ``user`` is one of the step's contributors.

    Says nothing about oversight (see ``oversees_step``) nor about whether
    the step is active. Nobody on the applicant team contributes to the
    proposal's evaluation, whatever role brought them to it.
    """
    call_step = instance.call_step
    proposal = instance.proposal
    if call_step.kind != StepKinds.APPLICANT_ACTION and (
        proposal_roles.is_on_applicant_team(user, proposal)
    ):
        return False
    try:
        source, parameter = parse_contributor_source(call_step.contributor_source)
    except ValueError:
        return False
    if source == ContributorSources.NONE:
        return proposal_permissions.user_holds_step_role(user, proposal, call_step)
    if source == ContributorSources.REQUESTED_OFFERING_TEAM:
        return proposal_permissions.offering_manager_user_ids_for_proposal(
            proposal, call_step
        ).filter(user=user).exists() or (
            call_step.per_requested_offering
            and not proposal_roles.recorded_conflicts(user, proposal).exists()
            and instance.contributions.filter(
                requested_offering__offering_id__in=offering_ids_managed_by(user)
            ).exists()
        )
    if source == ContributorSources.ASSIGNED_REVIEWERS:
        # A slot of theirs counts while it does: not once declined,
        # withdrawn or expired, nor while a conflict of interest stands.
        holds_slot = (
            instance.contributions.filter(contributor=user)
            .exclude(status__in=Statuses.CLOSED)
            .exists()
            and not proposal_roles.recorded_conflicts(user, proposal).exists()
        )
        return holds_slot or proposal_permissions.user_holds_step_role(
            user, proposal, call_step
        )
    if source == ContributorSources.CALL_ROLE:
        if parameter == ResponsibleRoles.PANEL_CHAIR:
            return proposal_permissions.chairs_panel_for(user, proposal)
        role_name = proposal_permissions.RESPONSIBLE_ROLE_TO_CALL_ROLE.get(parameter)
        return bool(
            role_name
            and proposal_permissions.holds_call_role_on_proposal(
                user, proposal, role_name
            )
        )
    if source == ContributorSources.PROPOSAL_ROLE:
        return step_role_contributors(proposal, call_step).filter(pk=user.pk).exists()
    return False


def peers_read_each_other(call_step) -> bool:
    """Whether a step's contributors read each other's contributions.

    Outside a blind review, the offering teams assessing a proposal as a
    whole -- the managers of the requested offerings see their peers'
    technical assessments -- and the holders of an evaluator role who
    complete the step they contribute to, such as the rapporteurs drafting
    their questions together. Everyone else reads their own. Reviewers judge
    independently, and a slot per requested offering is its team's alone.
    """
    if call_step.blind_review or call_step.per_requested_offering:
        return False
    source = call_step.contributor_source_kind
    if source == ContributorSources.REQUESTED_OFFERING_TEAM:
        return True
    if source == ContributorSources.PROPOSAL_ROLE:
        return _completer_is_a_contributor(call_step)
    return (
        source == ContributorSources.NONE
        and call_step.completer == ResponsibleRoles.OFFERING_MANAGER
    )


def _completer_is_a_contributor(call_step) -> bool:
    """Whether completing the step is its contributors' own role.

    The offering managers completing a technical assessment are its
    contributors, as are the reviewers completing a review step and the
    rapporteurs completing the reports they write; holding that role makes
    nobody an overseer of the others' work.
    """
    try:
        source, parameter = parse_contributor_source(call_step.contributor_source)
    except ValueError:
        return True
    role = call_step.completer
    if source == ContributorSources.NONE:
        return True
    if source == ContributorSources.REQUESTED_OFFERING_TEAM:
        return role == ResponsibleRoles.OFFERING_MANAGER
    if source == ContributorSources.ASSIGNED_REVIEWERS:
        return role == ResponsibleRoles.REVIEWER
    if source == ContributorSources.CALL_ROLE:
        return role == parameter
    if source == ContributorSources.PROPOSAL_ROLE:
        if role != ResponsibleRoles.PROPOSAL_ROLE:
            return False
        contributing = proposal_roles.resolve_role(
            call_step.call, call_step.contributor_source
        )
        return (
            contributing is None
            or contributing.pk == call_step.completer_proposal_role_id
        )
    return False


def _completer_oversees(user, instance) -> bool:
    """Whether ``user`` completes the step without contributing to it.

    A step whose completer role is not its contributors' -- a panel
    completing a step the offering teams contribute to, say -- is overseen
    by the holders of that role. Whoever is also one of the step's
    contributors oversees nothing: on a blind step they read their own
    contribution, like every other contributor.
    """
    call_step = instance.call_step
    if call_step.completer == ResponsibleRoles.APPLICANT:
        return False
    if _completer_is_a_contributor(call_step):
        return False
    if not proposal_permissions.user_holds_step_role(
        user, instance.proposal, call_step
    ):
        return False
    return not may_contribute(user, instance)


# =============================================================================
# Reading earlier steps' outputs
# =============================================================================

# A step reads its input steps once its work has started.
STARTED_STEP_STATUSES = (
    WorkflowStepInstanceStatuses.ACTIVE,
    WorkflowStepInstanceStatuses.COMPLETED,
    WorkflowStepInstanceStatuses.EXPIRED,
)

# How a user reads an input step's outputs: as an evaluator of a later step,
# or as the applicant on a later applicant step.
EVALUATOR_SIDE = "evaluator"
APPLICANT_SIDE = "applicant"


def steps_reading(input_instance):
    """The proposal's started steps that take ``input_instance``'s step as an
    input and come after it in the proposal's path. A step of a post-award
    run is read within its run."""
    readers = models.ProposalWorkflowStepInstance.objects.filter(
        proposal_id=input_instance.proposal_id,
        input_steps=input_instance.call_step_id,
        status__in=STARTED_STEP_STATUSES,
    )
    if input_instance.run_id:
        readers = readers.filter(run_id=input_instance.run_id)
    return readers.filter(
        Q(order__gt=input_instance.order)
        | Q(order=input_instance.order, id__gt=input_instance.pk)
    ).select_related("call_step", "proposal__round__call")


def input_instances(instance):
    """The proposal's instances of ``instance``'s input steps -- as fixed
    when the proposal was submitted -- that come before it in the proposal's
    path, in that order. One the proposal skipped is not read. A step of a
    post-award run reads the evaluation and its own run, never another run.
    """
    input_step_ids = list(instance.input_steps.values_list("id", flat=True))
    if not input_step_ids:
        return models.ProposalWorkflowStepInstance.objects.none()
    sources = models.ProposalWorkflowStepInstance.objects.filter(
        proposal_id=instance.proposal_id, call_step_id__in=input_step_ids
    )
    if instance.run_id:
        sources = sources.filter(Q(run__isnull=True) | Q(run_id=instance.run_id))
    else:
        sources = sources.filter(run__isnull=True)
    return (
        sources.exclude(status=WorkflowStepInstanceStatuses.SKIPPED)
        .filter(
            Q(order__lt=instance.order) | Q(order=instance.order, id__lt=instance.pk)
        )
        .select_related(
            "call_step__checklist",
            "call_step__completer_proposal_role",
            "proposal__round__call",
        )
        .order_by("order", "id")
    )


def holds_step_work(user, instance) -> bool:
    """Whether ``user`` holds the work of the proposal's step ``instance``.

    On an applicant step, anyone on the applicant team. Elsewhere its
    contributors and its completer as they stand now -- holders of an
    evaluator role through the role resolver, so not once revoked or while a
    conflict of interest stands -- and never someone on the applicant team.
    A contribution left over from before does not count: on a step of
    assigned reviewers only a review still in the work does.
    """
    proposal = instance.proposal
    call_step = instance.call_step
    on_team = proposal_roles.is_on_applicant_team(user, proposal)
    if call_step.kind == StepKinds.APPLICANT_ACTION:
        return on_team
    if on_team:
        return False
    if proposal_permissions.user_holds_step_role(user, proposal, call_step):
        return True
    if call_step.delegable_to and accepted_delegations(user, instance):
        # The work of a slot handed to them, once they accepted it.
        return True
    if call_step.contributor_source_kind == ContributorSources.ASSIGNED_REVIEWERS:
        return (
            instance.contributions.filter(contributor=user)
            .exclude(status__in=Statuses.CLOSED)
            .exists()
        )
    if call_step.contributors_lead_only:
        # Only the lead writes; every holder of the role still works on
        # the step, reading what it reads.
        return proposal_roles.holds_role(user, proposal, call_step.contributor_source)
    return may_contribute(user, instance)


def takes_part_in_step(user, instance, access=None) -> bool:
    """Whether ``user`` takes part in the proposal's step ``instance``.

    They hold its work -- complete it or contribute to it, through a call
    role or an evaluator role (``holds_step_work``) -- or hold the work of a
    later started step that reads it as an input. Says nothing about
    oversight: the call's managers read every step without taking part.
    """
    access = access or Access(user)
    return access.holds_step_work(instance) or access.input_side(instance) is not None


def is_input_output(contribution, side, access=None) -> bool:
    """Whether ``contribution`` is part of its step's output for ``side``.

    Only what was submitted -- on a step without contributors, the form its
    completer submitted by completing it. For evaluators, every slot of a
    per-offering step, as for the applicant -- except a provider assessing
    one of the step's offerings, who reads only the slots of their own,
    unless they oversee the call (``access`` says which). For the applicant only the
    public part: the submitted contributions of a step with contributors of
    its own. A reviewer's form, and the form of whoever completes a step
    without contributors, stay with the call team; the reviews themselves
    are shown in their applicant-safe form. Per-offering conclusions wait
    for a held decision like everything else about it.
    """
    instance = contribution.proposal_step
    call_step = instance.call_step
    source = call_step.contributor_source_kind
    if contribution.status != Statuses.SUBMITTED:
        return False
    if side == APPLICANT_SIDE:
        if source in (ContributorSources.NONE, ContributorSources.ASSIGNED_REVIEWERS):
            return False
        return not (contribution.is_per_offering and instance.proposal.decision_held)
    if contribution.is_per_offering:
        if access is None:
            return False
        if (
            access.user.is_support
            or access.oversees(instance.proposal)
            or access.on_team(contribution.requested_offering)
        ):
            return True
        # One provider does not read another's assessment of its partition;
        # the evaluators of later steps, such as rapporteurs, read them all.
        return not assesses_a_slot_of(access, instance)
    return True


def assesses_a_slot_of(access, instance) -> bool:
    """Whether ``access``'s user is on the team of any offering with a slot on
    the per-offering step ``instance``."""
    offering_ids = access._cached(
        ("slot_offerings", instance.pk),
        lambda: frozenset(
            instance.contributions.filter(requested_offering__isnull=False).values_list(
                "requested_offering__offering_id", flat=True
            )
        ),
    )
    return bool(offering_ids & access.managed_offering_ids())


class Access:
    """What one user may do with contributions, worked out once per request.

    Listing a step's contributions asks the same questions of every row --
    does the user oversee the step, is the user on this offering's team --
    so the answers are kept per step and per call rather than asked again.
    """

    def __init__(self, user, staff=None):
        self.user = user
        # Whether the request acts as staff (``has_staff_access``): a staff
        # user's personal access token without the staff scope does not.
        self.staff = user.is_staff if staff is None else staff
        self._memo = {}

    def _cached(self, key, compute):
        if key not in self._memo:
            self._memo[key] = compute()
        return self._memo[key]

    def managed_offering_ids(self):
        return self._cached(
            "offerings", lambda: frozenset(offering_ids_managed_by(self.user))
        )

    def on_team(self, requested_offering) -> bool:
        return requested_offering.offering_id in self.managed_offering_ids()

    def offering_scope(self, proposal):
        """The offerings whose part of ``proposal`` the user reads, or None
        for all of it (``permissions.offering_scope``)."""
        return self._cached(
            ("offering_scope", proposal.pk),
            lambda: proposal_permissions.offering_scope(self.user, proposal),
        )

    def award_scope(self, proposal):
        """The offerings whose part of ``proposal``'s award the user reads,
        or None for all of it (``permissions.award_scope``)."""
        return self._cached(
            ("award_scope", proposal.pk),
            lambda: proposal_permissions.award_scope(self.user, proposal),
        )

    def oversees(self, proposal) -> bool:
        """Staff and the call's managers and organisers, who may act on any
        contribution -- never on a proposal whose applicant team they are on
        (``permissions.oversees_as_call_team``)."""
        if self.on_applicant_team(proposal):
            return False
        if self.staff:
            return True
        call = proposal.round.call
        return self._cached(
            ("oversees", call.pk),
            lambda: proposal_permissions.manages_call_workflow(self.user, call),
        )

    def reads_all(self, instance) -> bool:
        """Whether the user reads every contribution of the step.

        Staff and support, the call's managers, and a completer of the step
        who is not one of its contributors.
        """
        if self.staff or self.user.is_support:
            return True
        if self.oversees(instance.proposal):
            return True
        return self._cached(
            ("completer", instance.pk),
            lambda: _completer_oversees(self.user, instance),
        )

    def may_contribute(self, instance) -> bool:
        return self._cached(
            ("contributes", instance.pk),
            lambda: may_contribute(self.user, instance),
        )

    def on_applicant_team(self, proposal) -> bool:
        return self._cached(
            ("applicant", proposal.pk),
            lambda: proposal_roles.is_on_applicant_team(self.user, proposal),
        )

    def holds_step_work(self, instance) -> bool:
        return self._cached(
            ("work", instance.pk), lambda: holds_step_work(self.user, instance)
        )

    def input_side(self, input_instance):
        """How the user reads ``input_instance``'s outputs as the input of a
        later step whose work they hold: ``EVALUATOR_SIDE``,
        ``APPLICANT_SIDE`` or None. An evaluator's reading wins."""

        def compute():
            side = None
            for later in steps_reading(input_instance):
                if not self.holds_step_work(later):
                    continue
                if later.call_step.kind != StepKinds.APPLICANT_ACTION:
                    return EVALUATOR_SIDE
                side = APPLICANT_SIDE
            return side

        return self._cached(("input", input_instance.pk), compute)

    def reads_as_applicant_response(self, contribution) -> bool:
        """Whether the user reads ``contribution`` as the applicant team's
        submitted answer on an applicant step, such as a response to
        reviews: one member submits it, the whole team reads it."""
        instance = contribution.proposal_step
        return (
            instance.call_step.kind == StepKinds.APPLICANT_ACTION
            and contribution.status == Statuses.SUBMITTED
            and self.on_applicant_team(instance.proposal)
        )

    def reads_as_released(self, contribution) -> bool:
        """Whether the user reads ``contribution`` as part of a report
        released to the applicant: the applicant team, once released as it
        was written, and not while the decision is held."""
        instance = contribution.proposal_step
        proposal = instance.proposal
        return (
            instance.released_at is not None
            and instance.call_step.release_to_applicant
            and not instance.released_text
            and contribution.status == Statuses.SUBMITTED
            and not proposal.decision_held
            and self.on_applicant_team(proposal)
        )

    def reads_as_input(self, contribution) -> bool:
        """Whether the user reads ``contribution`` as an output of a step
        that feeds a later step whose work they hold."""
        side = self.input_side(contribution.proposal_step)
        return side is not None and is_input_output(contribution, side, self)

    def delegate_id(self, contribution):
        """The id of the person the slot is handed to while they may take
        it over (``effective_delegate_id``), else None."""
        return self._cached(
            ("delegate", contribution.pk), lambda: effective_delegate_id(contribution)
        )

    def is_delegate(self, contribution) -> bool:
        """Whether the slot is handed to the user, accepted or not."""
        return self.delegate_id(contribution) == self.user.pk

    def is_filler(self, contribution) -> bool:
        """Whether the user fills the contribution.

        The person it is handed to, once they accepted it (and on the
        contribution they submitted): its holders then only read it. Else
        its holders (``owns_slot``).
        """
        delegate_id = self.delegate_id(contribution)
        if delegate_id is not None:
            return delegate_id == self.user.pk and contribution.status in (
                Statuses.ACCEPTED,
                Statuses.SUBMITTED,
            )
        return self.owns_slot(contribution)

    def owns_slot(self, contribution) -> bool:
        """Whether the contribution is the user's own, or their team's.

        Never an evaluation's for someone on the applicant team: an offering
        manager who joins it no longer fills their offering's slot. Nor, on a
        step of the holders of a role, for someone who no longer holds it --
        revoked, recused, or with a conflict of interest on record: the slot
        stays theirs on record, but is no longer theirs to fill or read.
        """
        instance = contribution.proposal_step
        if instance.call_step.kind != StepKinds.APPLICANT_ACTION and (
            self.on_applicant_team(instance.proposal)
        ):
            return False
        if contribution.is_per_offering:
            return self.on_team(contribution.requested_offering)
        if contribution.contributor_id != self.user.id:
            return False
        if instance.call_step.contributor_source_kind in ROLE_SOURCES:
            return self.may_contribute(instance)
        return True

    def can_fill(self, contribution) -> bool:
        """Whether the user may answer and submit ``contribution``.

        Never once it is submitted. Staff and the call's managers otherwise
        always may. A contributor only while the step is active and the
        contribution open: a per-offering slot is anyone on that offering's
        team, a per-person slot its contributor alone.
        """
        instance = contribution.proposal_step
        if contribution.status == Statuses.SUBMITTED:
            # Unless changes asked of the team unlock the answers of a
            # post-award run's applicant step: its own fillers revise them.
            return run_form_unlocked(instance) and self.is_filler(contribution)

        if self.oversees(instance.proposal):
            return True
        if instance.status != WorkflowStepInstanceStatuses.ACTIVE:
            return False
        if contribution.status not in Statuses.OPEN:
            return False
        return self.is_filler(contribution)

    def can_read(self, contribution) -> bool:
        """Whether the user may read ``contribution`` and its answers.

        Those who read every contribution of the step (``reads_all``), and
        its fillers. A per-offering slot is nobody else's business but the
        applicant's, whatever ``blind_review`` says. A per-person slot is
        also its peers', where peers read each other's work
        (``peers_read_each_other``).

        The applicant team reads its own work on an applicant step, drafts
        included. Of the evaluation, it reads what was submitted to a step
        shown to applicants when the call shows its reviews to submitters,
        as the step list shows their verdicts -- never the outcome of a
        per-offering slot while the decision is held for the round's
        publication -- and a released report (``reads_as_released``). Never
        a contribution still being written: its outcome, comment and answers
        are not the evaluators' word yet.
        """
        instance = contribution.proposal_step
        proposal = instance.proposal
        if self.reads_all(instance) or self.is_filler(contribution):
            return True
        if self.owns_slot(contribution):
            # Handed to someone else, the slot is still its holders' to read.
            return True
        if self.reads_as_input(contribution):
            return True
        if self.reads_as_released(contribution):
            return True
        if self.reads_as_applicant_response(contribution):
            return True
        if self.on_applicant_team(proposal):
            if instance.call_step.kind == StepKinds.APPLICANT_ACTION:
                return contribution.status not in Statuses.CLOSED
            return (
                instance.call_step.applicant_visible
                and contribution.status == Statuses.SUBMITTED
                and proposal.round.call.reviews_visible_to_submitters
                and not (contribution.is_per_offering and proposal.decision_held)
            )
        if contribution.is_per_offering:
            return False
        return peers_read_each_other(instance.call_step) and self.may_contribute(
            instance
        )

    def conceals_contributor(self, contribution) -> bool:
        """Whether who contributed is hidden from the user.

        From the applicant team, unless the call reveals reviewer identity
        to submitters, as on the reviews; the applicant's own contribution, and
        a step the applicant completes, are theirs to know. From the
        evaluators of a later step reading a blind review as its input:
        those reading every contribution of the step -- the call's managers,
        staff -- still know who wrote what.
        """
        instance = contribution.proposal_step
        proposal = instance.proposal
        if self.reads_all(instance):
            return False
        if contribution.contributor_id == self.user.id:
            return False
        if not self.on_applicant_team(proposal):
            return instance.call_step.blind_review
        if instance.call_step.completer == ResponsibleRoles.APPLICANT:
            return False
        return not proposal.round.call.reviewer_identity_visible_to_submitters


# Where people are kept from who wrote what, they are numbered in an order
# that says nothing about them: not when they were assigned, took the work
# on or wrote it, nor where they stand on another step.
ANONYMOUS_ORDER = ("uuid",)


def anonymous_labels(proposal):
    """Stable labels for the people evaluating ``proposal``, for readers who
    are not to know who they are.

    ``(review_labels, contributor_labels)``: a review by its number among
    the proposal's reviews, as ``ProposalReviewSerializer`` labels them
    ("Reviewer 2"), keyed by review id; any other contribution by its
    number among the contributions to its own step ("Contributor 1"), keyed
    by contribution id. Both are numbered in ``ANONYMOUS_ORDER``, so a label
    stays the same from one reading to the next.
    """
    review_labels = {
        pk: gettext_lazy("Reviewer %(index)s") % {"index": index}
        for index, pk in enumerate(
            proposal.review_set.order_by(*ANONYMOUS_ORDER).values_list("pk", flat=True),
            start=1,
        )
    }
    contributor_labels = {}
    numbers = {}
    for pk, step_id in (
        models.StepContribution.objects.filter(
            proposal_step__proposal=proposal,
            contributor__isnull=False,
            review__isnull=True,
        )
        .exclude(proposal_step__call_step__kind=StepKinds.APPLICANT_ACTION)
        .order_by("proposal_step_id", *ANONYMOUS_ORDER)
        .values_list("pk", "proposal_step_id")
    ):
        numbers[step_id] = numbers.get(step_id, 0) + 1
        contributor_labels[pk] = gettext_lazy("Contributor %(index)s") % {
            "index": numbers[step_id]
        }
    return review_labels, contributor_labels


def anonymous_label(contribution, labels) -> str:
    """The label ``contribution``'s contributor goes by where concealed;
    ``labels`` is ``anonymous_labels`` of its proposal."""
    review_labels, contributor_labels = labels
    if contribution.review_id and contribution.review_id in review_labels:
        return str(review_labels[contribution.review_id])
    return str(contributor_labels.get(contribution.pk, gettext_lazy("Contributor")))


def listing_order(call_step):
    """How a step's contributions are listed: in ``ANONYMOUS_ORDER`` on a
    blind step, whose readers may not know who wrote them, so the place of
    one in the list tells no more than its label; by when they were made
    otherwise."""
    if call_step.blind_review:
        return ANONYMOUS_ORDER
    return ("created", "id")


def review_is_blind(review) -> bool:
    """Whether ``review`` was written in a blind review: the step it was
    given in, else the call's review step, keeps reviewers from each other."""
    step = review_step(review)
    return bool(step and step.blind_review)


def review_step(review):
    """The call step ``review`` is given in: its contribution's step, else the
    call's review step. None when the call has neither."""
    try:
        return review.contribution.proposal_step.call_step
    except models.StepContribution.DoesNotExist:
        pass
    return models.CallWorkflowStep.review_step_for(review.proposal.round.call)


def review_criteria(review) -> list:
    """The criteria the reviewer scores ``review`` on, in their order."""
    step = review_step(review)
    return list(step.criteria.all()) if step else []


def criterion_key(criterion) -> str:
    """The key of ``criterion`` in a review's ``criteria_scores``."""
    return UUID(str(criterion.uuid)).hex


def weighted_review_score(criteria, scores: dict) -> int | None:
    """The overall score ``scores`` give a review on ``criteria``: their
    average weighted by each criterion's weight, rounded half up.

    Criteria without a score are left out. None when none is scored.
    """
    weighted = [
        (criterion.weight, scores[criterion_key(criterion)])
        for criterion in criteria
        if scores.get(criterion_key(criterion))
    ]
    if not weighted:
        return None
    total_weight = sum(weight for weight, _ in weighted)
    total = sum(weight * score for weight, score in weighted)
    # Whole numbers throughout, so that x.5 rounds up exactly.
    return (2 * total + total_weight) // (2 * total_weight)


def reviews_in_step(step):
    """The reviews given in ``step``: those of its contributions, and -- when
    it is its call's review step -- those that belong to no step's
    contribution, which ``review_step`` places there."""
    in_step = Q(contribution__proposal_step__call_step=step)
    review_step_of_call = models.CallWorkflowStep.review_step_for(step.call)
    if review_step_of_call and review_step_of_call.pk == step.pk:
        in_step |= Q(contribution__isnull=True, proposal__round__call_id=step.call_id)
    return models.Review.objects.filter(in_step)


def step_has_scored_reviews(step) -> bool:
    """Whether a reviewer has scored ``step``'s criteria, or submitted a
    review in it: from then on its criteria stand as the reviewers saw them."""
    return (
        reviews_in_step(step)
        .filter(
            ~Q(criteria_scores={}) | Q(state=models.Review.States.SUBMITTED),
        )
        .exists()
    )


def access_for(request):
    """The ``Access`` of the request's user, made once per request."""
    access = getattr(request, "_contribution_access", None)
    if access is None or access.user != request.user:
        access = Access(request.user, has_staff_access(request))
        request._contribution_access = access
    return access


def _may_open_own_slot(user, instance) -> bool:
    """Whether a per-person slot is made for ``user`` the first time they ask.

    Never on a per-offering step, whose slots are the offerings'. On a step
    of assigned reviewers a reviewer's slot comes with the review they took
    on; the holders of the step's completer role may still fill its form,
    but only reviews count towards its gates.
    """
    call_step = instance.call_step
    if call_step.per_requested_offering:
        return False
    if oversees_step(user, instance.proposal):
        return True
    return may_contribute(user, instance)


# =============================================================================
# Slots
# =============================================================================


def _get_or_create_person_slot(instance, user, **defaults):
    defaults.setdefault("due_at", instance.deadline)
    lookup = {
        "proposal_step": instance,
        "contributor": user,
        "requested_offering": None,
        "review": None,
    }
    try:
        with transaction.atomic():
            contribution, _ = models.StepContribution.objects.get_or_create(
                **lookup, defaults=defaults
            )
    except IntegrityError:
        contribution = models.StepContribution.objects.get(**lookup)
    return contribution


# =============================================================================
# Run conditions
# =============================================================================


def run_condition_match(instance):
    """Whether ``instance``'s run condition holds on its proposal, and the
    requested offerings it holds for.

    Returns ``(holds, requested_offering_ids)``. A step without a condition
    always runs, for every offering (``None``). Otherwise the condition step
    -- the proposal's instance of the step it names -- must have ended with
    one of its outcomes, or one of its counted contributions must have. When
    per-offering contributions match, the ids of their requested offerings
    say which offerings the step runs for; ``None`` when the step's own
    outcome matched, or contributions not tied to an offering did. A
    condition step that was skipped or lapsed holds only for a condition
    naming ``expired``; one that never ended, or was withdrawn, never holds.
    """
    condition = instance.run_condition or {}
    if not condition:
        return True, None
    if SCORE_SPREAD_ABOVE in condition:
        return _scores_diverge(instance, condition), None
    outcomes = set(condition.get("outcomes") or [])
    source = (
        models.ProposalWorkflowStepInstance.objects.filter(
            proposal_id=instance.proposal_id, step=condition.get("step")
        )
        .select_related("call_step", "proposal")
        .first()
    )
    if source is None:
        return False, None
    if source.status in (
        WorkflowStepInstanceStatuses.SKIPPED,
        WorkflowStepInstanceStatuses.EXPIRED,
    ):
        return WorkflowStepOutcomes.EXPIRED in outcomes, None
    if source.status != WorkflowStepInstanceStatuses.COMPLETED:
        return False, None
    matched = counted_contributions(source).filter(outcome__in=outcomes)
    offering_ids = set(
        matched.filter(requested_offering__isnull=False).values_list(
            "requested_offering_id", flat=True
        )
    )
    if offering_ids:
        return True, offering_ids
    if matched.exists() or source.outcome in outcomes:
        return True, None
    return False, None


def _scores_diverge(instance, condition) -> bool:
    """Whether the scores the condition step's counted contributions gave
    the proposal lie more than the condition's points apart. Fewer than two
    scores never diverge; nor does a step that did not complete."""
    source = (
        models.ProposalWorkflowStepInstance.objects.filter(
            proposal_id=instance.proposal_id, step=condition.get("step")
        )
        .select_related("call_step", "proposal")
        .first()
    )
    if source is None or source.status != WorkflowStepInstanceStatuses.COMPLETED:
        return False
    spread = score_spread(source)
    return spread is not None and spread > float(condition[SCORE_SPREAD_ABOVE])


def run_condition_holds(instance) -> bool:
    return run_condition_match(instance)[0]


def create_contributions(instance):
    """Create the slots a step's contributors are known by when it starts.

    One per accepted requested offering on a per-offering step -- of a
    conditional step, only for the offerings its condition held for -- one
    per holder of the evaluator role a ``proposal_role`` step names. Slots
    made earlier (a review taken on before the step started) get the step's
    deadline. Returns the step's contributions.
    """
    call_step = instance.call_step
    proposal = instance.proposal
    try:
        source, _ = parse_contributor_source(call_step.contributor_source)
    except ValueError:
        source = ContributorSources.NONE
    if call_step.per_requested_offering:
        requested_offerings = requested_offerings_of(proposal, call_step)
        _, matched_offering_ids = run_condition_match(instance)
        if matched_offering_ids is not None:
            # Only narrows: never an offering the proposal does not request.
            requested_offerings = requested_offerings.filter(
                id__in=matched_offering_ids
            )
        for requested_offering in requested_offerings:
            slot, created = models.StepContribution.objects.get_or_create(
                proposal_step=instance,
                requested_offering=requested_offering,
                defaults={"due_at": instance.deadline},
            )
            if created:
                hand_to_default_delegate(slot)
    elif source == ContributorSources.PROPOSAL_ROLE:
        for user in step_role_contributors(proposal, call_step):
            _get_or_create_person_slot(instance, user)
    if instance.deadline:
        instance.contributions.filter(due_at__isnull=True).update(
            due_at=instance.deadline
        )
    return instance.contributions.all()


def hand_to_default_delegate(slot):
    """Hand a new per-offering slot to its offering's default delegate, when
    the provider named one, the step lets the slot be handed within the
    offering's team, and the delegate may still take it."""
    requested_offering = slot.requested_offering
    if requested_offering is None or requested_offering.default_delegate_id is None:
        return
    target, _ = delegation_target(slot.proposal_step.call_step)
    if target != DelegationTargets.OFFERING_TEAM:
        return
    delegate_user = requested_offering.default_delegate
    if not is_eligible_delegate(delegate_user, slot):
        return
    slot.delegate = delegate_user
    slot.delegated_by = None
    slot.delegated_at = timezone.now()
    slot.save(update_fields=[*DELEGATION_FIELDS, "modified"])
    if dry_run_context.current() is not None:
        return
    # tasks imports this module; notification_rules defers the same import.
    from waldur_mastermind.proposal import tasks

    uuid_hex, delegate_uuid = slot.uuid.hex, delegate_user.uuid.hex
    transaction.on_commit(
        lambda: tasks.notify_contribution_delegated.delay(
            uuid_hex, tasks.DelegationEvents.DELEGATED, delegate_uuid, None
        )
    )


# A slot whose contributor no longer may fill it -- their role revoked or
# lapsed, a conflict of interest recorded, the lead moved to another holder
# -- lapses with their standing. Unlike one the call's managers withdrew or
# its contributor declined, a lapsed slot comes back once they may
# contribute again.
LAPSED = Statuses.EXPIRED


def sync_lead_slots(proposal, role):
    """Move the slot of the proposal's active lead-only steps of ``role`` to
    the role's current lead.

    Called once the lead may have changed: a holder marked as the lead, the
    lead revoked, or the lead's conflict-of-interest confirmation in. The
    open slots of the role's other holders, past and present, lapse
    (``LAPSED``); what they submitted stays. The lead gets a slot, or the
    one that lapsed from them earlier back -- not one the call's managers
    withdrew.
    """
    instances = models.ProposalWorkflowStepInstance.objects.filter(
        proposal=proposal,
        status=WorkflowStepInstanceStatuses.ACTIVE,
        call_step__contributors_lead_only=True,
    ).select_related("call_step")
    call = proposal.round.call
    for instance in instances:
        call_step = instance.call_step
        named = proposal_roles.resolve_role(call, call_step.contributor_source)
        if named is None or named.pk != role.pk:
            continue
        leads = list(step_role_contributors(proposal, call_step))
        lead_ids = [lead.pk for lead in leads]
        holder_ids = (
            proposal_roles.assignment_history(proposal)
            .filter(role=role)
            .values_list("user_id", flat=True)
        )
        now = timezone.now()
        instance.contributions.filter(
            contributor_id__in=holder_ids,
            requested_offering__isnull=True,
            review__isnull=True,
            status__in=Statuses.OPEN,
        ).exclude(contributor_id__in=lead_ids).update(status=LAPSED, modified=now)
        for lead in leads:
            slot = _get_or_create_person_slot(instance, lead)
            if slot.status == LAPSED:
                slot.status = Statuses.PENDING
                slot.save(update_fields=["status", "modified"])


def sync_role_slots(user, proposal):
    """Bring ``user``'s slots on the proposal's steps of an evaluator role or
    a call role in line with whether they may contribute to them now.

    Called once that may have changed: their role revoked or given, a
    conflict of interest recorded, recused for, dismissed or waived, their
    confirmation of having none in. An open slot of someone who may no
    longer contribute lapses (``LAPSED``); what they submitted stays on
    record (see ``counted_contributions`` for whether it still counts). A
    slot that lapsed from someone who may contribute again reopens while its
    step is active. A slot the call's managers withdrew stays withdrawn, and
    one they declined themselves stays declined. Returns whether any slot
    changed.
    """
    slots = (
        models.StepContribution.objects.filter(
            proposal_step__proposal=proposal,
            contributor=user,
            requested_offering__isnull=True,
            review__isnull=True,
            status__in=(*Statuses.OPEN, LAPSED),
        )
        .filter(
            Q(
                proposal_step__call_step__contributor_source__startswith=(
                    f"{ContributorSources.PROPOSAL_ROLE}:"
                )
            )
            | Q(
                proposal_step__call_step__contributor_source__startswith=(
                    f"{ContributorSources.CALL_ROLE}:"
                )
            )
        )
        .select_related("proposal_step__call_step", "proposal_step__proposal")
    )
    withdrawn, reopened = [], []
    for slot in slots:
        instance = slot.proposal_step
        if slot.status in Statuses.OPEN:
            if not may_contribute(user, instance):
                withdrawn.append(slot.pk)
        elif instance.status == WorkflowStepInstanceStatuses.ACTIVE and (
            may_contribute(user, instance)
        ):
            reopened.append(slot.pk)
    now = timezone.now()
    # Each only while the slot still stands as it was read: one submitted,
    # or a step decided, meanwhile is left as it is.
    changed = 0
    if withdrawn:
        changed += models.StepContribution.objects.filter(
            pk__in=withdrawn, status__in=Statuses.OPEN
        ).update(status=LAPSED, modified=now)
    if reopened:
        changed += models.StepContribution.objects.filter(
            pk__in=reopened,
            status=LAPSED,
            proposal_step__status=WorkflowStepInstanceStatuses.ACTIVE,
        ).update(status=Statuses.PENDING, modified=now)
    return bool(changed)


def fillable_contributions(user, instance):
    """The step's contributions ``user`` may fill as a contributor. An
    offering's slot is its team's, unless a conflict of interest recorded
    with the proposal keeps them off it."""
    team_slots = (
        Q(pk__in=[])
        if proposal_roles.recorded_conflicts(user, instance.proposal).exists()
        else Q(requested_offering__offering_id__in=offering_ids_managed_by(user))
    )
    return instance.contributions.filter(
        Q(contributor=user, requested_offering__isnull=True)
        | team_slots
        | Q(delegate=user)
    )


def resolve_contribution(user, instance, contribution_uuid=None, create=True):
    """The contribution ``user`` acts on in a step, or None.

    Named by ``contribution_uuid``: that contribution of the step. Otherwise
    the caller's own: the one open contribution they may fill, else the one
    they filled last, else -- where the step makes slots on demand and
    ``create`` is set -- a new one of their own. Raises ``ContributionError``
    when the caller may fill several open contributions and names none.
    """
    if contribution_uuid:
        try:
            return instance.contributions.filter(uuid=contribution_uuid).first()
        except ValueError:
            return None
    own = fillable_contributions(user, instance)
    open_ones = list(own.filter(status__in=Statuses.OPEN)[:2])
    if len(open_ones) > 1:
        raise ContributionError(
            "You may fill several contributions to this step; name one with "
            "contribution_uuid."
        )
    if open_ones:
        return open_ones[0]
    latest = own.order_by("-modified").first()
    if latest is not None:
        return latest
    if create and _may_open_own_slot(user, instance):
        return _get_or_create_person_slot(instance, user)
    return None


# =============================================================================
# Permissions on one contribution
# =============================================================================


def run_form_unlocked(instance) -> bool:
    """Whether the open request for changes in ``instance``'s post-award run
    unlocks the answers of ``instance``, the run's applicant step."""
    return bool(
        instance.run_id
        and instance.call_step.kind == StepKinds.APPLICANT_ACTION
        and change_requests.run_part_unlocked(instance.run, ChangeRequestParts.RUN_FORM)
    )


def user_can_fill(user, contribution) -> bool:
    """Whether ``user`` may answer and submit ``contribution``; see ``Access``."""
    return Access(user).can_fill(contribution)


def user_can_read(user, contribution) -> bool:
    """Whether ``user`` may read ``contribution`` and its answers; see ``Access``."""
    return Access(user).can_read(contribution)


def readable_contributions(
    user, proposal, instance=None, access=None, with_handed_over=False
):
    """The proposal's contributions ``user`` may read, by step order.

    Their forms' completions and answers come loaded, so listing them costs
    the same few queries however many there are. ``with_handed_over`` adds
    the slots handed to the user that they have yet to accept -- for the
    list of contributions, never with their answers.
    """
    access = access or Access(user)
    contributions = (
        models.StepContribution.objects.filter(proposal_step__proposal=proposal)
        .select_related(
            "proposal_step__call_step__checklist",
            "proposal_step__proposal__round__call",
            "requested_offering__offering",
            "contributor",
            "delegate",
            "review",
        )
        .order_by("proposal_step__order", "proposal_step_id", "created", "id")
    )
    if instance is not None:
        contributions = contributions.filter(proposal_step=instance)
    readable = [
        c
        for c in contributions
        if access.can_read(c) or (with_handed_over and access.is_delegate(c))
    ]
    # Within a blind step in its anonymous order (see ``listing_order``);
    # the sort is stable, so other steps keep theirs.
    readable.sort(
        key=lambda c: (
            c.proposal_step.order,
            c.proposal_step_id,
            c.uuid.hex if c.proposal_step.call_step.blind_review else "",
        )
    )
    prefetch_answers(readable)
    return readable


# =============================================================================
# Answers
# =============================================================================


def prefetch_answers(contributions):
    """Load the form completions and answers of ``contributions`` in bulk.

    ``get_checklist_completion``, ``latest_answers`` and
    ``unanswered_required_count`` then answer from what was loaded.
    """
    if not contributions:
        return
    completions = checklist_models.ChecklistCompletion.objects.filter(
        scope_content_type=ContentType.objects.get_for_model(models.StepContribution),
        scope_object_id__in=[c.pk for c in contributions],
    ).select_related("checklist")
    by_scope = {}
    for completion in completions:
        completion._prefetched_answers = []
        by_scope.setdefault(completion.scope_object_id, {})[completion.checklist_id] = (
            completion
        )
    answers = checklist_models.Answer.objects.filter(
        completion__in=[c for scoped in by_scope.values() for c in scoped.values()]
    ).select_related("question", "user")
    completions_by_id = {
        completion.pk: completion
        for scoped in by_scope.values()
        for completion in scoped.values()
    }
    for answer in answers:
        completions_by_id[answer.completion_id]._prefetched_answers.append(answer)
    checklist_ids = {
        c.proposal_step.call_step.checklist_id
        for c in contributions
        if c.proposal_step.call_step.checklist_id
    }
    required = {checklist_id: set() for checklist_id in checklist_ids}
    for checklist_id, question_id in checklist_models.Question.objects.filter(
        checklist_id__in=checklist_ids, required=True
    ).values_list("checklist_id", "id"):
        required[checklist_id].add(question_id)
    for contribution in contributions:
        contribution._checklist_completions = by_scope.get(contribution.pk, {})
        contribution._required_question_ids = required


def latest_answers(completion):
    """The answer standing for each question of ``completion``."""
    prefetched = getattr(completion, "_prefetched_answers", None)
    if prefetched is None:
        return completion.get_latest_answers()
    return checklist_utils.latest_answers_by_question(prefetched)


def unanswered_required_count(contribution):
    """How many required questions of the step's form the contribution leaves open."""
    checklist = contribution.proposal_step.call_step.checklist
    if checklist is None:
        return 0
    cached = getattr(contribution, "_unanswered_required_count", None)
    if cached is not None:
        return cached
    completion = contribution.get_checklist_completion(checklist)
    required = getattr(contribution, "_required_question_ids", {}).get(checklist.id)
    if required is not None:
        answered = (
            {answer.question_id for answer in completion._prefetched_answers}
            if completion is not None
            else set()
        )
        count = len(required - answered)
    elif completion is None:
        count = checklist.questions.filter(required=True).count()
    else:
        count = completion.get_unanswered_required_questions().count()
    contribution._unanswered_required_count = count
    return count


def contribution_completions(instance):
    """Completions of the step's form held by its contributions still counting."""
    checklist_id = instance.call_step.checklist_id
    contribution_ids = _excluding_conflicted(
        instance.contributions.exclude(status__in=Statuses.CLOSED), instance
    ).values_list("id", flat=True)
    return checklist_models.ChecklistCompletion.objects.filter(
        scope_content_type=ContentType.objects.get_for_model(models.StepContribution),
        scope_object_id__in=contribution_ids,
        checklist_id=checklist_id,
    )


def _rating_value(answer):
    """The number a ``rating`` answer gives, or None."""
    value = answer.answer_data
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def step_score(instance):
    """The score ``instance``'s counted contributions gave the proposal.

    The mean summary score of their submitted reviews; failing those, the
    mean of the ``rating`` answers in their completions of the step's form
    (a consensus report grades the proposal that way); else None. The
    individual answers stay the step's: only the mean leaves here.
    """
    counted = counted_contributions(instance)
    score = models.Review.objects.filter(
        contribution__in=counted, state=models.Review.States.SUBMITTED
    ).aggregate(avg=Avg("summary_score"))["avg"]
    if score is not None:
        return score
    checklist_id = instance.call_step.checklist_id
    if not checklist_id:
        return None
    completions = checklist_models.ChecklistCompletion.objects.filter(
        scope_content_type=ContentType.objects.get_for_model(models.StepContribution),
        scope_object_id__in=counted.values("id"),
        checklist_id=checklist_id,
    )
    ratings = [
        value
        for completion in completions
        for answer in latest_answers(completion).values()
        if answer.question.question_type == QuestionTypes.RATING
        and (value := _rating_value(answer)) is not None
    ]
    if not ratings:
        return None
    return sum(ratings) / len(ratings)


def contribution_scores(instance) -> list[float]:
    """The score each of ``instance``'s counted contributions gave the
    proposal, as ``step_score`` reads them: a submitted review's summary
    score, else the mean of the ``rating`` answers in the contribution's
    completion of the step's form. Contributions without one are left out."""
    counted = list(counted_contributions(instance))
    if not counted:
        return []
    scores = {}
    for review in models.Review.objects.filter(
        contribution__in=counted, state=models.Review.States.SUBMITTED
    ).select_related("contribution"):
        scores[review.contribution.pk] = float(review.summary_score)
    checklist_id = instance.call_step.checklist_id
    if checklist_id:
        unscored = [c.pk for c in counted if c.pk not in scores]
        completions = checklist_models.ChecklistCompletion.objects.filter(
            scope_content_type=ContentType.objects.get_for_model(
                models.StepContribution
            ),
            scope_object_id__in=unscored,
            checklist_id=checklist_id,
        )
        for completion in completions:
            ratings = [
                value
                for answer in latest_answers(completion).values()
                if answer.question.question_type == QuestionTypes.RATING
                and (value := _rating_value(answer)) is not None
            ]
            if ratings:
                scores[completion.scope_object_id] = sum(ratings) / len(ratings)
    return [scores[c.pk] for c in counted if c.pk in scores]


def score_spread(instance) -> float | None:
    """How far apart the scores of ``instance``'s contributions lie: the
    highest less the lowest; None with fewer than two."""
    scores = contribution_scores(instance)
    if len(scores) < 2:
        return None
    return max(scores) - min(scores)


def unanswered_required_questions(instance):
    """Required questions of the step's form no contribution has answered.

    A step whose form is the call's compliance checklist also counts the
    applicant's answers to it, which stay on the proposal: before steps had
    contributions, such a step read exactly those.
    """
    checklist = instance.call_step.checklist
    completions = contribution_completions(instance)
    answered = checklist_models.Answer.objects.filter(completion__in=completions)
    proposal = instance.proposal
    if checklist.id == proposal.round.call.compliance_checklist_id:
        compliance = proposal.get_checklist_completion_for(checklist)
        if compliance is not None:
            answered = answered | checklist_models.Answer.objects.filter(
                completion=compliance
            )
    return checklist.questions.filter(required=True).exclude(
        id__in=answered.values_list("question_id", flat=True)
    )


def adopt_proposal_answers(proposal):
    """Move answers to a step's form kept on the proposal into contributions.

    Answers written before steps had contributions -- an older export, a
    demo preset -- sit on the proposal's completion of the form, told apart
    by their user. Each answering user gets a contribution of their own on
    the proposal's first step using that form, and their answers move to it
    unchanged: submitted on a step that is over, open on one still to
    finish. The applicant's compliance answers stay on the proposal.
    Each proposal's answers move together or not at all. Returns the
    number of answers moved.
    """
    call = proposal.round.call
    completions = checklist_models.ChecklistCompletion.objects.filter(
        scope_content_type=ContentType.objects.get_for_model(models.Proposal),
        scope_object_id=proposal.id,
    )
    if call.compliance_checklist_id:
        completions = completions.exclude(checklist_id=call.compliance_checklist_id)
    moved = 0
    with transaction.atomic():
        for completion in completions:
            moved += _adopt_completion(proposal, completion)
    return moved


ANSWER_COPY_FIELDS = (
    "question_id",
    "answer_data",
    "requires_review",
    "reviewed_by_id",
    "reviewed_at",
    "review_notes",
)


def _adopt_completion(proposal, completion):
    # Every step using the form read the proposal's answers to it, so each
    # that ran gets them: the first step's completion takes them, the others
    # a copy.
    instances = list(
        proposal.workflow_step_instances.filter(
            call_step__checklist_id=completion.checklist_id
        )
        .exclude(status=WorkflowStepInstanceStatuses.SKIPPED)
        .order_by("order", "id")
    )
    if not instances:
        return 0
    moved = 0
    answers = checklist_models.Answer.objects.filter(completion=completion)
    for user_id in answers.values_list("user_id", flat=True).distinct():
        first_target = None
        for instance in instances:
            target = _adopting_completion(instance, user_id, completion.checklist)
            if first_target is None:
                first_target = target
                moved += answers.filter(user_id=user_id).update(completion=target)
            else:
                _copy_answers(first_target, target, user_id)
            target.update_completion_status()
    if not answers.exists():
        completion.delete()
    return moved


def _adopting_completion(instance, user_id, checklist):
    """``user_id``'s completion of ``checklist`` in their contribution to
    ``instance``, both made if missing."""
    finished = instance.status in (
        WorkflowStepInstanceStatuses.COMPLETED,
        WorkflowStepInstanceStatuses.EXPIRED,
    )
    # By id: the answers of a user deactivated since still move.
    contribution, _ = models.StepContribution.objects.get_or_create(
        proposal_step=instance,
        contributor_id=user_id,
        requested_offering=None,
        review=None,
        defaults={
            "due_at": instance.deadline,
            "status": Statuses.SUBMITTED if finished else Statuses.PENDING,
            "submitted_at": timezone.now() if finished else None,
        },
    )
    return contribution.ensure_checklist_completion(checklist)


def _copy_answers(source, target, user_id):
    """Copy ``user_id``'s answers from one completion to another verbatim:
    bulk_create, so Answer.save() does not reprocess them."""
    have = set(
        target.answers.filter(user_id=user_id).values_list("question_id", flat=True)
    )
    checklist_models.Answer.objects.bulk_create(
        checklist_models.Answer(
            completion=target,
            user_id=user_id,
            **{field: getattr(answer, field) for field in ANSWER_COPY_FIELDS},
        )
        for answer in source.answers.filter(user_id=user_id)
        if answer.question_id not in have
    )


# =============================================================================
# Submitting
# =============================================================================


def derive_step_outcome(instance):
    """The step outcome its counted contributions add up to, or None.

    Any rejected contribution makes the step infeasible; accepted and
    conditionally accepted ones make it feasible. Only those that count
    (``counted_contributions``) have a say.
    """
    outcomes = set(counted_contributions(instance).values_list("outcome", flat=True))
    if not outcomes:
        return None
    if outcomes & ContributionOutcomes.NEGATIVE:
        return WorkflowStepOutcomes.INFEASIBLE
    return WorkflowStepOutcomes.FEASIBLE


def _validate_outcome(call_step, outcome, conditions):
    allowed = call_step.contribution_outcomes
    if allowed:
        if not outcome:
            raise ContributionError(
                f"An outcome is required: one of {', '.join(allowed)}."
            )
        if outcome not in allowed:
            raise ContributionError(
                f"'{outcome}' is not an outcome of this step's contributions; "
                f"use one of {', '.join(allowed)}."
            )
        if (
            outcome == ContributionOutcomes.CONDITIONALLY_ACCEPTED
            and not (conditions or "").strip()
        ):
            raise ContributionError("A conditional acceptance needs its conditions.")
        return
    if outcome and outcome not in call_step.allowed_outcomes:
        raise ContributionError(
            f"'{outcome}' is not an outcome of this step; use one of "
            f"{', '.join(call_step.allowed_outcomes)}."
        )


def _announce_submitted(contribution):
    # notification_rules reads the permissions module, which reads this one.
    from waldur_mastermind.proposal import notification_rules

    notification_rules.dispatch_contribution_submitted(contribution)


def submit(contribution, user, outcome="", conditions="", comment=""):
    """Submit a contribution with its outcome; the caller holds its row lock.

    A per-offering slot becomes the submitter's. The step's form must be
    answered first when it is required, and a step that takes at most
    ``max_contributions`` refuses one more.
    """
    instance = contribution.proposal_step
    call_step = instance.call_step
    if contribution.status not in Statuses.OPEN:
        raise ContributionError(
            f"The contribution is {contribution.status} and cannot be submitted."
        )
    _validate_outcome(call_step, outcome, conditions)
    if call_step.checklist_id and call_step.checklist_required:
        completion = contribution.get_checklist_completion(call_step.checklist)
        unanswered = (
            completion.get_unanswered_required_questions()
            if completion is not None
            else call_step.checklist.questions.filter(required=True)
        )
        count = unanswered.count()
        if count:
            raise ContributionError(
                f"{count} required "
                f"{'question is' if count == 1 else 'questions are'} unanswered."
            )
    if call_step.max_contributions is not None:
        submitted = counted_contributions(instance).exclude(pk=contribution.pk).count()
        if submitted >= call_step.max_contributions:
            raise ContributionError(
                f"The step takes at most {call_step.max_contributions} "
                "contributions, and has them."
            )
    contribution.status = Statuses.SUBMITTED
    contribution.outcome = outcome or ""
    contribution.conditions = conditions or ""
    contribution.comment = comment or ""
    contribution.submitted_at = timezone.now()
    if contribution.contributor_id is None:
        contribution.contributor = user
    if contribution.delegate_id is not None and contribution.delegate_id != user.pk:
        # Someone else wrote it -- a call manager in the delegate's place, or
        # its holders once the delegate was no longer eligible: the record
        # says who it was handed to only when they wrote it.
        _clear_delegation(contribution)
    contribution.save(
        update_fields=[
            "status",
            "outcome",
            "conditions",
            "comment",
            "submitted_at",
            "contributor",
            *DELEGATION_FIELDS,
            "modified",
        ]
    )
    # A reopened contribution handed in again closes its reopening.
    change_requests.close_open_of_contributions(
        [contribution], ChangeRequestResolutions.SUBMITTED, user
    )
    _announce_submitted(contribution)
    return contribution


def decline(contribution, user, comment=""):
    """Take a contribution out of the step's work.

    Its contributor declines a per-person slot; the call's managers withdraw
    any open contribution. A per-offering slot is the offering's, so its
    team cannot decline it.
    """
    if contribution.status not in Statuses.OPEN:
        raise ContributionError(
            f"The contribution is {contribution.status} and cannot be declined."
        )
    if contribution.delegate_id is not None and contribution.delegate_id == user.pk:
        # The person it was handed to hands it back to its holders.
        _clear_delegation(contribution)
        contribution.status = Statuses.PENDING
        contribution.save(update_fields=["status", *DELEGATION_FIELDS, "modified"])
        return contribution
    if oversees_step(user, contribution.proposal_step.proposal):
        contribution.status = Statuses.WITHDRAWN
    elif not contribution.is_per_offering and contribution.contributor_id == user.id:
        contribution.status = Statuses.DECLINED
    else:
        raise ContributionError(
            "Only the call's managers can withdraw a per-offering contribution."
        )
    if comment:
        contribution.comment = comment
    contribution.save(update_fields=["status", "comment", "modified"])
    change_requests.close_open_of_contributions(
        [contribution], ChangeRequestResolutions.WITHDRAWN, user
    )
    return contribution


# =============================================================================
# Delegating a slot
# =============================================================================

DELEGATION_FIELDS = (
    "delegate",
    "delegated_by",
    "delegated_at",
    "coi_declaration_text",
)


def delegation_target(call_step):
    """``(target, parameter)`` of who the step's slots can be handed to; a
    value that names nobody known hands them to nobody."""
    try:
        return parse_delegation_target(call_step.delegable_to)
    except ValueError:
        return DelegationTargets.NONE, None


def is_eligible_delegate(user, contribution) -> bool:
    """Whether ``user`` may take ``contribution`` over, as the step's
    ``delegable_to`` says: on a per-offering step, anyone with a role on the
    slot's own offering -- never on another one the proposal requests; on a
    step of per-person slots, a holder of the evaluator role it names on the
    proposal other than the slot's own holder. Never someone on the applicant
    team or with a conflict of interest with the proposal on record.

    Asked again on every use, so whoever loses their standing loses the slot
    with it, and the slot returns to its holders.
    """
    if user is None or user.is_anonymous or not user.is_active:
        return False
    if contribution.review_id is not None:
        return False
    instance = contribution.proposal_step
    call_step = instance.call_step
    proposal = instance.proposal
    target, _ = delegation_target(call_step)
    if target == DelegationTargets.NONE:
        return False
    if target == DelegationTargets.OFFERING_TEAM and not contribution.is_per_offering:
        return False
    if target == DelegationTargets.PROPOSAL_ROLE and (
        contribution.is_per_offering or contribution.contributor_id == user.pk
    ):
        return False
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if proposal_roles.recorded_conflicts(user, proposal).exists():
        return False
    if target == DelegationTargets.OFFERING_TEAM:
        return (
            offering_role_holder_ids(contribution.requested_offering.offering_id)
            .filter(user_id=user.pk)
            .exists()
        )
    return (
        proposal_role_holders(proposal, call_step.delegable_to)
        .filter(pk=user.pk)
        .exists()
    )


def effective_delegate_id(contribution):
    """The id of the person ``contribution`` is handed to while they may
    take it over, else None: the slot is then its holders' again."""
    if contribution.delegate_id is None:
        return None
    if not is_eligible_delegate(contribution.delegate, contribution):
        return None
    return contribution.delegate_id


def accepted_delegations(user, instance):
    """The open slots of the step ``instance`` handed to ``user`` that they
    accepted and may still take over."""
    slots = list(instance.contributions.filter(delegate=user, status=Statuses.ACCEPTED))
    for slot in slots:
        slot.proposal_step = instance
    return [slot for slot in slots if effective_delegate_id(slot) == user.pk]


def may_delegate(user, contribution, access=None) -> bool:
    """Whether ``user`` may hand ``contribution`` to someone, or take it back.

    An open slot of an active step that can be handed on: by staff and the
    call's managers, and by its holders -- the offering's team, the person
    whose slot it is. Not by the person it is handed to: one hop only.
    """
    access = access or Access(user)
    instance = contribution.proposal_step
    if delegation_target(instance.call_step)[0] == DelegationTargets.NONE:
        return False
    if contribution.review_id is not None:
        return False
    if instance.status != WorkflowStepInstanceStatuses.ACTIVE:
        return False
    if contribution.status not in Statuses.OPEN:
        return False
    if access.oversees(instance.proposal):
        return True
    if access.is_delegate(contribution):
        return False
    return access.owns_slot(contribution)


def may_accept(user, contribution, access=None) -> bool:
    """Whether ``user`` is the one ``contribution`` is handed to and has yet
    to accept it, on an active step."""
    access = access or Access(user)
    return (
        contribution.status == Statuses.PENDING
        and contribution.proposal_step.status == WorkflowStepInstanceStatuses.ACTIVE
        and access.is_delegate(contribution)
    )


def delegation_candidates(contribution):
    """The people ``contribution`` can be handed to (``is_eligible_delegate``),
    by name: whoever may hand it on picks one of them. Its current delegate
    stays in the list."""
    instance = contribution.proposal_step
    call_step = instance.call_step
    proposal = instance.proposal
    target, _ = delegation_target(call_step)
    if contribution.review_id is not None:
        return User.objects.none()
    if target == DelegationTargets.OFFERING_TEAM and contribution.is_per_offering:
        users = User.objects.filter(
            id__in=offering_role_holder_ids(contribution.requested_offering.offering_id)
        )
    elif target == DelegationTargets.PROPOSAL_ROLE and not contribution.is_per_offering:
        users = proposal_role_holders(proposal, call_step.delegable_to)
        if contribution.contributor_id:
            users = users.exclude(pk=contribution.contributor_id)
    else:
        return User.objects.none()
    return (
        users.filter(is_active=True)
        .exclude(id__in=conflicted_contributor_ids(proposal))
        .order_by("first_name", "last_name", "username")
    )


def _clear_delegation(contribution):
    contribution.delegate = None
    contribution.delegated_by = None
    contribution.delegated_at = None
    contribution.coi_declaration_text = ""


def delegate(contribution, user, delegate_user):
    """Hand ``contribution`` to ``delegate_user``, or take it back when
    None; the caller holds its row lock. Returns the person it was handed to
    before, if any.

    The slot waits for the delegate to accept it (pending). What was
    written in it stays.
    """
    if contribution.status not in Statuses.OPEN:
        raise ContributionError(
            f"The contribution is {contribution.status} and cannot be handed on."
        )
    previous = contribution.delegate if contribution.delegate_id else None
    if delegate_user is None:
        if previous is None:
            raise ContributionError("The contribution is not handed to anyone.")
        _clear_delegation(contribution)
    else:
        if delegate_user.pk == contribution.delegate_id:
            raise ContributionError("The contribution is already handed to them.")
        if not is_eligible_delegate(delegate_user, contribution):
            raise ContributionError("This user cannot take the contribution over.")
        contribution.delegate = delegate_user
        contribution.delegated_by = user
        contribution.delegated_at = timezone.now()
        # The new delegate has declared nothing yet.
        contribution.coi_declaration_text = ""
    contribution.status = Statuses.PENDING
    contribution.save(update_fields=["status", *DELEGATION_FIELDS, "modified"])
    return previous


def accept_delegation(contribution, user, confirms_no_conflict=False):
    """The person ``contribution`` is handed to takes it over; the caller
    holds its row lock. A step asking its evaluators to confirm having no
    conflict of interest asks them too."""
    if not may_accept(user, contribution):
        raise ContributionError(
            "The contribution is not handed to you, or you already accepted it."
        )
    if contribution.proposal_step.call_step.requires_coi_confirmation and not (
        confirms_no_conflict
    ):
        raise ContributionError(
            "Confirm that you have no conflict of interest with the proposal "
            "to take the contribution over."
        )
    contribution.status = Statuses.ACCEPTED
    # The declaration confirmed, as worded now.
    contribution.coi_declaration_text = coi_declarations.for_contribution(contribution)
    contribution.save(update_fields=["status", "coi_declaration_text", "modified"])
    coi_declarations.record_contribution_confirmation(contribution, user)
    return contribution


def submit_completer_form(instance, user):
    """Submit the form ``user`` filled on a step without contributors as they
    complete it: completing such a step is submitting its form, the step's
    output -- a response to reviews, say."""
    if user is None or instance.call_step.collects_contributions:
        return
    now = timezone.now()
    instance.contributions.filter(
        contributor=user,
        requested_offering__isnull=True,
        review__isnull=True,
        status__in=Statuses.OPEN,
    ).update(status=Statuses.SUBMITTED, submitted_at=now, modified=now)


def withdraw_open(instance):
    """Withdraw the step's open contributions, once the step is decided."""
    open_ = instance.contributions.filter(status__in=Statuses.OPEN)
    change_requests.close_open_of_contributions(
        open_, ChangeRequestResolutions.CANCELLED
    )
    open_.update(status=Statuses.WITHDRAWN, modified=timezone.now())


def withdraw_open_of(user, proposal):
    """Withdraw ``user``'s open contributions to the proposal's evaluation,
    once they may no longer evaluate it. A per-offering slot is its
    offering team's, not theirs, and stays open for the rest of the team."""
    open_ = models.StepContribution.objects.filter(
        proposal_step__proposal=proposal,
        contributor=user,
        requested_offering__isnull=True,
        status__in=Statuses.OPEN,
    ).exclude(proposal_step__call_step__kind=StepKinds.APPLICANT_ACTION)
    change_requests.close_open_of_contributions(
        open_, ChangeRequestResolutions.WITHDRAWN
    )
    open_.update(status=Statuses.WITHDRAWN, modified=timezone.now())


def expire_open(instance):
    """Mark the step's open contributions expired, with the step."""
    open_ = instance.contributions.filter(status__in=Statuses.OPEN)
    change_requests.close_open_of_contributions(open_, ChangeRequestResolutions.LAPSED)
    open_.update(status=Statuses.EXPIRED, modified=timezone.now())


# =============================================================================
# Reviews
# =============================================================================

REVIEW_STATUSES = {
    models.Review.States.IN_REVIEW: Statuses.ACCEPTED,
    models.Review.States.SUBMITTED: Statuses.SUBMITTED,
    models.Review.States.REJECTED: Statuses.DECLINED,
}


def review_step_instance(proposal, call_step=None):
    """The proposal's step a review contributes to, or None.

    ``call_step`` when the review came from an assignment batch for it.
    Otherwise the proposal's step whose contributors are the assigned
    reviewers: the active one, else the next one to start, else the first.
    """
    instances = (
        proposal.workflow_step_instances.select_related("call_step")
        .exclude(status=WorkflowStepInstanceStatuses.SKIPPED)
        .order_by("order", "id")
    )
    if call_step is not None:
        instance = instances.filter(call_step=call_step).first()
        if instance is not None:
            return instance
    reviewing = instances.filter(
        call_step__contributor_source=ContributorSources.ASSIGNED_REVIEWERS
    )
    return (
        reviewing.filter(status=WorkflowStepInstanceStatuses.ACTIVE).first()
        or reviewing.filter(status=WorkflowStepInstanceStatuses.PENDING).first()
        or reviewing.first()
    )


def _review_contribution_values(review):
    status = REVIEW_STATUSES.get(review.state, Statuses.ACCEPTED)
    return {
        "status": status,
        "submitted_at": (
            (review.submitted_at or review.modified)
            if status == Statuses.SUBMITTED
            else None
        ),
    }


def attach_review(review, assignment_item=None):
    """Give a review its contribution, or bring its contribution up to date.

    A review taken on from an assignment contributes to the step its batch
    was made for; any other to the proposal's review step. A proposal not
    submitted yet has no steps, and its reviews no contributions.
    """
    contribution = models.StepContribution.objects.filter(review=review).first()
    batch_step = assignment_item.batch.call_step if assignment_item else None
    if contribution is None:
        instance = review_step_instance(review.proposal, batch_step)
        if instance is None:
            return None
        contribution = models.StepContribution.objects.create(
            proposal_step=instance,
            contributor_id=review.reviewer_id,
            review=review,
            assignment_item=assignment_item,
            due_at=instance.deadline,
            **_review_contribution_values(review),
        )
        _announce_submitted(contribution)
        return contribution
    was_submitted = contribution.status == Statuses.SUBMITTED
    update_fields = []
    if assignment_item is not None and contribution.assignment_item_id is None:
        contribution.assignment_item = assignment_item
        update_fields.append("assignment_item")
        if batch_step is not None:
            instance = review_step_instance(review.proposal, batch_step)
            if instance is not None and instance.pk != contribution.proposal_step_id:
                contribution.proposal_step = instance
                contribution.due_at = instance.deadline
                update_fields += ["proposal_step", "due_at"]
    if contribution.status not in (Statuses.WITHDRAWN, Statuses.EXPIRED):
        for field, value in _review_contribution_values(review).items():
            if getattr(contribution, field) != value and not (
                field == "submitted_at" and value and contribution.submitted_at
            ):
                setattr(contribution, field, value)
                update_fields.append(field)
    if update_fields:
        contribution.save(update_fields=[*update_fields, "modified"])
        if not was_submitted:
            _announce_submitted(contribution)
    return contribution


# =============================================================================
# Per-offering outcomes for the allocation decision
# =============================================================================


def offering_assessments(user, proposal, access=None):
    """The per-offering outcomes and conditions ``user`` may read, by step.

    Whoever manages the call reads them all, for the allocation decision;
    everyone else what they may read of the contributions (``Access``).
    """
    access = access or Access(user)
    decides = proposal_permissions._user_manages_the_call(user, proposal)
    rows = (
        models.StepContribution.objects.filter(
            proposal_step__proposal=proposal, requested_offering__isnull=False
        )
        .select_related(
            "proposal_step__call_step",
            "proposal_step__proposal__round__call",
            "requested_offering__offering",
        )
        .order_by("proposal_step__order", "proposal_step_id", "created", "id")
    )
    return [
        contribution
        for contribution in rows
        if contribution.proposal_step.call_step.derives_outcome
        and (decides or access.can_read(contribution))
    ]


# -- why a step waits ----------------------------------------------------------

_CALL_ROLE_LABELS = {
    ResponsibleRoles.REVIEWER: "reviewer",
    ResponsibleRoles.PANEL_MEMBER: "panel member",
}


def _teamless_open_offerings(instance):
    """Names of the offerings whose open per-offering slot nobody can fill:
    no one is on the offering's team."""
    slots = instance.contributions.filter(
        requested_offering__isnull=False, status__in=Statuses.OPEN
    ).select_related("requested_offering__offering")
    offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
    staffed = set(
        permissions_models.UserRole.objects.filter(
            is_active=True,
            user__is_active=True,
            role__name=RoleEnum.OFFERING_MANAGER,
            content_type=offering_ct,
            object_id__in=[s.requested_offering.offering_id for s in slots],
        ).values_list("object_id", flat=True)
    )
    return [
        slot.requested_offering.offering.name
        for slot in slots
        if slot.requested_offering.offering_id not in staffed
    ]


def _completer_missing(instance) -> str | None:
    """Why nobody holding the step's completer role can complete it, or None."""
    call_step = instance.call_step
    proposal = instance.proposal
    completer = call_step.completer
    if completer in _CALL_ROLE_LABELS:
        role_name = proposal_permissions.RESPONSIBLE_ROLE_TO_CALL_ROLE[completer]
        if permission_utils.get_users(
            proposal.round.call, role_name=role_name
        ).exists():
            return None
        label = _CALL_ROLE_LABELS[completer]
        return (
            f"Nobody holds the {label} role on the call, so nobody but a call "
            f"manager can complete this step: add a {label} in the call's Team "
            "tab, or complete the step as a call manager."
        )
    if completer == ResponsibleRoles.PANEL_CHAIR:
        if proposal_permissions.panel_chair_ids(proposal.round.call, proposal):
            return None
        return (
            "The call has no panel chair or vice-chair able to act on this "
            "proposal, so nobody but a call manager can complete this step: "
            "set the panel chair in the call's Team tab, or complete the step "
            "as a call manager."
        )
    if completer == ResponsibleRoles.OFFERING_MANAGER:
        if proposal_permissions.offering_manager_user_ids_for_proposal(
            proposal, call_step
        ).exists():
            return None
        return (
            "No offering the proposal requests has a team, so nobody but a call "
            "manager can complete this step."
        )
    if completer == ResponsibleRoles.PROPOSAL_ROLE:
        role = call_step.completer_proposal_role
        if (
            role is None
            or proposal_roles.proposal_role_holders(proposal, role).exists()
        ):
            return None
        return (
            f"Nobody holds the role '{role.name}' on this proposal, so nobody "
            "but a call manager can complete this step: assign a holder, or "
            "complete the step as a call manager."
        )
    return None


def why_step_waits(instance) -> str | None:
    """Why the active step ``instance`` cannot move on without someone being
    added or acting in another's place, or None when its own people can.

    Each reason names the way forward, for the call manager who reads it on
    the proposal. A step simply waiting for its contributors' work is not
    stalled and gives no reason.
    """
    if instance.status != WorkflowStepInstanceStatuses.ACTIVE:
        return None
    call_step = instance.call_step
    proposal = instance.proposal
    if call_step.round_scoped:
        return round_reports.why_round_step_waits(instance)
    for reason in (
        proposal_roles.missing_holders(proposal, call_step),
        proposal_roles.missing_lead(proposal, call_step),
    ):
        if reason:
            return reason
    if call_step.per_requested_offering:
        teamless = _teamless_open_offerings(instance)
        if teamless:
            return (
                f"Nobody is on the team of {', '.join(teamless)} to assess it: "
                "a call manager can submit the assessment in its place."
            )
    source = call_step.contributor_source_kind
    if (
        source == ContributorSources.PROPOSAL_ROLE
        and not call_step.contributors_lead_only
        and not instance.contributions.filter(contributor__isnull=False).exists()
    ):
        role = proposal_roles.resolve_role(
            proposal.round.call, call_step.contributor_source
        )
        if (
            role is not None
            and not step_role_contributors(proposal, call_step).exists()
        ):
            holders = list(
                proposal_roles.with_standing_conflict(
                    proposal_roles.active_assignments(proposal, role)
                )
            )
            if holders and all(h.has_standing_conflict for h in holders):
                return (
                    f"Every holder of the role '{role.name}' has a recorded "
                    "conflict of interest with this proposal: replace them, or "
                    "dismiss or waive the conflicts."
                )
            if holders:
                return (
                    f"No holder of the role '{role.name}' can contribute yet: "
                    "they have not yet confirmed having no conflict of interest."
                )
            return (
                f"Nobody holds the role '{role.name}' on this proposal to "
                "contribute: assign a holder."
            )
    if (
        source == ContributorSources.ASSIGNED_REVIEWERS
        and call_step.min_contributions
        and not instance.contributions.filter(status__in=Statuses.OPEN).exists()
    ):
        submitted = models.Review.objects.filter(
            contribution__proposal_step=instance,
            contribution__status=Statuses.SUBMITTED,
            state=models.Review.States.SUBMITTED,
        ).count()
        if submitted < call_step.min_contributions:
            return (
                f"{submitted} of {call_step.min_contributions} required reviews "
                "are in and no reviewer is working on the proposal: assign more "
                "reviewers to it with Create review."
            )
    return _completer_missing(instance)


def reviewers_short_ahead(instance) -> str | None:
    """The shortfall of reviewers a later step of the proposal will meet,
    for the call manager completing the reviewer assignment: the first later
    step taking reviews from assigned reviewers needs more than the
    reviewers assigned to the proposal so far. None when it does not."""
    if (
        instance.status != WorkflowStepInstanceStatuses.ACTIVE
        or instance.call_step.kind != StepKinds.ASSIGNMENT
    ):
        return None
    later = (
        instance.proposal.workflow_step_instances.filter(
            status=WorkflowStepInstanceStatuses.PENDING,
            order__gt=instance.order,
            call_step__contributor_source=ContributorSources.ASSIGNED_REVIEWERS,
            call_step__min_contributions__gt=0,
        )
        .select_related("call_step")
        .order_by("order", "id")
        .first()
    )
    if later is None:
        return None
    needed = later.call_step.min_contributions
    assigned = (
        models.Review.objects.filter(proposal=instance.proposal)
        .exclude(state=models.Review.States.REJECTED)
        .values("reviewer_id")
        .distinct()
        .count()
    )
    if assigned >= needed:
        return None
    return (
        f"{later.call_step.name} needs {needed} "
        f"{'review' if needed == 1 else 'reviews'}; {assigned} "
        f"{'reviewer is' if assigned == 1 else 'reviewers are'} assigned."
    )
