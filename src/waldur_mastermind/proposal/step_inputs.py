"""What a step reads of earlier steps, and the reports released to the applicant.

A call step names earlier steps of its call as its ``input_steps``. Whoever
holds the step's work on a proposal -- its contributors and completer, or the
applicant team on an applicant step -- reads what those steps produced for the
same proposal: their contributions with their answers, the reviews linked to
them and their outcome. ``step_outputs`` is the one place that is resolved,
filtered for the viewer; ``contributions.Access`` gives the same people the
contributions' answers and files.

The applicant side reads only the public parts: the reviews without their
private comment, and without the reviewer when the call conceals reviewers;
the submitted contributions of a step with contributors; never internal notes
or a reviewer's own form. A step's outcome stays hidden where the proposal's
step list hides it.

An assessment step configured ``release_to_applicant`` -- a consolidated
report, final feedback -- is released to the applicant once completed, by its
completer or a call manager, with an edited text if they wish. Released, it
is the applicant's to read whatever the call says about reviews; before, it
is not.
"""

import dataclasses
import logging

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import timezone

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import get_scope_ids
from waldur_mastermind.proposal import contributions, models, proposal_roles
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    OutcomeSets,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    WorkflowStepInstanceStatuses,
)

logger = logging.getLogger(__name__)


class ReleaseError(ValueError):
    """The step's report cannot be released as asked."""


@dataclasses.dataclass
class StepOutput:
    """What one input step produced on a proposal, as one viewer may read it."""

    instance: models.ProposalWorkflowStepInstance
    contributions: list
    reviews: list
    applicant_side: bool


# -- who reads -----------------------------------------------------------------


def oversees_inputs(user, proposal) -> bool:
    """Staff, support and the call's managers read every step's inputs --
    never as the proposal's applicant team, which reads them only from the
    applicant's side."""
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    return (
        user.is_staff
        or user.is_support
        or contributions.Access(user).oversees(proposal)
        or proposal_permissions.oversees_proposal_call(user, proposal)
    )


def can_read_inputs(user, instance, access=None) -> bool:
    """Whether ``user`` reads the input steps' outputs of ``instance``.

    Those overseeing the call always; whoever holds the step's work once it
    has started, and afterwards.
    """
    if oversees_inputs(user, instance.proposal):
        return True
    if instance.status not in contributions.STARTED_STEP_STATUSES:
        return False
    access = access or contributions.Access(user)
    return access.holds_step_work(instance)


def reads_as_applicant(user, instance, access=None) -> bool:
    """Whether ``user`` reads ``instance``'s inputs in their applicant-safe form."""
    if oversees_inputs(user, instance.proposal):
        return False
    access = access or contributions.Access(user)
    return access.on_applicant_team(instance.proposal)


def _collects_reviews(call_step) -> bool:
    return (
        call_step.contributor_source_kind == ContributorSources.ASSIGNED_REVIEWERS
        or call_step.outcome_set == OutcomeSets.REVIEWED
    )


def _submitted_reviews(instance):
    """The submitted reviews of a step collecting reviews."""
    reviews = models.Review.objects.filter(
        proposal_id=instance.proposal_id, state=models.Review.States.SUBMITTED
    ).select_related("proposal__round__call__manager__customer", "reviewer")
    # In the order their anonymous labels number them, so the place of a
    # review in the list tells no more than its label.
    reviews = reviews.order_by(*contributions.ANONYMOUS_ORDER)
    linked = reviews.filter(contribution__proposal_step=instance)
    if linked.exists():
        return list(linked)
    # The proposal's reviews were never linked to a step: they are the
    # review step's when the call has one.
    review_step = models.CallWorkflowStep.review_step_for(instance.proposal.round.call)
    if review_step is not None and review_step.pk == instance.call_step_id:
        return list(reviews.filter(contribution__isnull=True))
    return []


def step_outputs(user, instance, access=None):
    """The outputs of ``instance``'s input steps that ``user`` may read.

    One ``StepOutput`` per input step the proposal walked before ``instance``,
    in path order. The caller checked ``can_read_inputs``.
    """
    access = access or contributions.Access(user)
    applicant_side = reads_as_applicant(user, instance, access)
    side = (
        contributions.APPLICANT_SIDE if applicant_side else contributions.EVALUATOR_SIDE
    )
    outputs = []
    for input_instance in contributions.input_instances(instance):
        if input_instance.status == WorkflowStepInstanceStatuses.PENDING:
            # Not reached yet: nothing to read.
            outputs.append(StepOutput(input_instance, [], [], applicant_side))
            continue
        candidates = list(
            input_instance.contributions.select_related(
                "proposal_step__call_step__checklist",
                "proposal_step__proposal__round__call",
                "requested_offering__offering",
                "contributor",
                "review",
            ).order_by(*contributions.listing_order(input_instance.call_step))
        )
        # A reviewer's contribution is their review, listed as such.
        readable = [
            contribution
            for contribution in candidates
            if contribution.review_id is None
            and contributions.is_input_output(contribution, side, access)
        ]
        contributions.prefetch_answers(readable)
        reviews = (
            _submitted_reviews(input_instance)
            if _collects_reviews(input_instance.call_step)
            else []
        )
        outputs.append(StepOutput(input_instance, readable, reviews, applicant_side))
    return outputs


# -- releasing a report --------------------------------------------------------


def can_release(user, instance) -> bool:
    """Whether ``user`` may release ``instance``'s report: its completer, or
    whoever oversees the call; never someone on the applicant team."""
    if user.is_staff:
        return True
    proposal = instance.proposal
    if proposal_roles.is_on_applicant_team(user, proposal):
        return False
    if proposal_permissions.oversees_proposal_call(user, proposal):
        return True
    call_step = instance.call_step
    if call_step.completer == ResponsibleRoles.APPLICANT:
        return False
    return proposal_permissions.user_completes_step(user, proposal, call_step)


def release(instance, user, text=""):
    """Release the report of the proposal's completed step to the applicant.

    Recorded once, with who released it, when, and the text the applicant
    reads in place of the report when one is given. The caller holds the
    proposal's and the step's row locks inside a transaction.
    """
    call_step = instance.call_step
    if call_step.kind != StepKinds.ASSESSMENT or not call_step.release_to_applicant:
        raise ReleaseError(
            f"The report of step '{call_step.name}' is not released to the applicant."
        )
    if instance.status != WorkflowStepInstanceStatuses.COMPLETED:
        raise ReleaseError(
            f"Step '{call_step.name}' is {instance.status}; its report is "
            "released once the step is completed."
        )
    if instance.released_at is not None:
        raise ReleaseError(
            f"The report of step '{call_step.name}' has already been released."
        )
    instance.released_at = timezone.now()
    instance.released_by = user
    instance.released_text = text or ""
    instance.save(update_fields=["released_at", "released_by", "released_text"])
    # Filed on the proposal's feed, where only those overseeing the call read
    # it (``proposals_hiding_report_releases_from``): the event's context
    # carries the releaser's name, username, IP address and user agent, and
    # the releaser may be a rapporteur whom the applicant team, the other
    # evaluators and the providers are not to know.
    event_logger.emit(
        "Report of step {step_name} on proposal {proposal_name} was released "
        "to the applicant by {actor}.",
        event_type=EventType.PROPOSAL_REPORT_RELEASED,
        event_context={
            "proposal": instance.proposal,
            "step_name": call_step.name,
            "actor": user.full_name or user.username,
        },
        scopes=[instance.proposal],
    )
    logger.info(
        "Report of step %s on proposal %s released to the applicant by user %s",
        instance.uuid.hex,
        instance.proposal.uuid.hex,
        user.uuid.hex,
    )
    return instance


def proposals_hiding_report_releases_from(user):
    """Proposals whose report releases ``user`` does not read on their feed.

    Who released a report is the call's own business: staff and support,
    and whoever holds CALL.UPDATE on the call or its managing organisation
    without being on the proposal's applicant team, read the event; nobody
    else does. The applicant team reads the report itself, without who
    released it. ``None`` when nothing is hidden.
    """
    if user.is_active and (user.is_staff or user.is_support):
        return None
    proposals = models.Proposal.objects.all()
    if not user.is_active:
        return proposals
    managed_calls = get_scope_ids(
        user,
        ContentType.objects.get_for_model(models.Call),
        permission=PermissionEnum.UPDATE_CALL,
    )
    organised_calls = get_scope_ids(
        user,
        ContentType.objects.get_for_model(models.CallManagingOrganisation),
        permission=PermissionEnum.UPDATE_CALL,
    )
    on_team = get_scope_ids(user, ContentType.objects.get_for_model(models.Proposal))
    return proposals.exclude(
        (
            Q(round__call_id__in=managed_calls)
            | Q(round__call__manager_id__in=organised_calls)
        )
        & ~Q(pk__in=on_team)
    )


def released_instances(proposal):
    """The proposal's step instances whose report has been released."""
    return (
        # A step no longer configured to release its report takes it back.
        proposal.workflow_step_instances.filter(
            released_at__isnull=False, call_step__release_to_applicant=True
        )
        .select_related("call_step__checklist", "released_by")
        .order_by("order", "id")
    )


def reads_as_call_team(user, proposal) -> bool:
    """Whether ``user`` reads the proposal's steps as its call team does:
    with internal notes, who acted and the evaluators' feedback.

    Whoever holds a call-wide role on the call, and the organisers of its managing
    organisation, who oversee the call without one -- never the applicant.
    """
    if proposal_permissions.user_can_view_internal_notes(user, proposal):
        return True
    return proposal_permissions.oversees_proposal_call(
        user, proposal
    ) and not proposal_roles.is_on_applicant_team(user, proposal)


def can_read_released_reports(user, proposal) -> bool:
    """Whether ``user`` reads the proposal's released reports as released:
    the call team always; the applicant team, and the call's reviewers and
    panel members who see the proposal, unless the decision is held for the
    round's publication. A reviewer or panel member also reads the report of
    a step they take part in as the call team does, whatever this says (see
    ``permissions.user_can_view_internal_notes``)."""
    if reads_as_call_team(user, proposal):
        return True
    if not (
        proposal_roles.is_on_applicant_team(user, proposal)
        or proposal_permissions.holds_call_role(user, proposal.round.call)
    ):
        return False
    return not proposal.decision_held


def report_contributions(instance):
    """The contributions making up a released step's report: what was
    submitted to the step."""
    report = list(
        instance.contributions.filter(status=StepContributionStatuses.SUBMITTED)
        .select_related(
            "proposal_step__call_step__checklist",
            "proposal_step__proposal__round__call",
            "requested_offering__offering",
            "contributor",
            "review",
        )
        .order_by("created", "id")
    )
    contributions.prefetch_answers(report)
    return report
