"""What the applicant may still edit on a proposal, and requests for changes.

A proposal's content is the applicant's to edit while it is a draft. Once it
is submitted, what was submitted is what gets evaluated -- except for the
parts the open requests for changes unlock (``models.ProposalChangeRequest``).
Every applicant edit path asks ``applicant_may_edit`` (or
``require_applicant_may_edit``), so a part is either open on all of them or
on none.

This module reads the models only, so the permission checks, serializers and
handlers can all use it. The workflow side of a request -- making, submitting,
cancelling and lapsing it -- is in ``workflow_service``; reopening a
contribution in ``contributions``.
"""

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from waldur_core.core.exceptions import IncorrectStateException
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    ChangeRequestParts,
    ProposalFieldStates,
    ProposalRunStates,
    ProposalStates,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
)

LOCKED_MESSAGE = _("Only proposals with a draft status are available for editing.")


def open_requests():
    return models.ProposalChangeRequest.objects.filter(closed_at__isnull=True)


def open_applicant_requests(proposal):
    """The open requests for changes to the applicant on ``proposal``,
    earliest first.

    Several steps of a stage may each ask at once; the applicant answers
    them together. Only while the proposal is in review, and only those of
    steps that still stand active: a request a step left behind unlocks
    nothing, closed or not.
    """
    requests = open_requests().filter(
        proposal=proposal,
        contribution__isnull=True,
        step_instance__status=WorkflowStepInstanceStatuses.ACTIVE,
    )
    if proposal.state == ProposalStates.IN_REVIEW:
        requests = requests.filter(step_instance__run__isnull=True)
    elif proposal.state == ProposalStates.ACCEPTED:
        # Asked at a step of the accepted proposal's open post-award run.
        requests = requests.filter(step_instance__run__state=ProposalRunStates.OPEN)
    else:
        return models.ProposalChangeRequest.objects.none()
    return requests.select_related(
        "step_instance__call_step", "step_instance__run", "requested_by"
    ).order_by("created", "id")


def unlocked_parts(proposal) -> set:
    """The parts of ``proposal`` an open request for changes unlocks: the
    union of what every open request names."""
    parts = set()
    for request_parts in open_applicant_requests(proposal).values_list(
        "unlocked_parts", flat=True
    ):
        parts.update(request_parts or [])
    return parts


def applicant_may_edit(proposal, part) -> bool:
    """Whether the applicant side may change ``part`` of ``proposal`` now.

    In draft, every part. Afterwards only a part one of the open requests
    for changes unlocks (``ChangeRequestParts``): a part stays editable
    while any request naming it is open. Says nothing about who is asking:
    the callers check that it is someone acting for the applicant.

    Inside a transaction -- every API write runs in one -- the proposal is
    locked and read afresh first, and stays locked until the write commits:
    submitting the changes, or the request lapsing, takes the same lock, so
    an edit cannot land after the request that allowed it has closed.
    """
    if proposal.pk is not None and transaction.get_connection().in_atomic_block:
        proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
            pk=proposal.pk
        )
    if proposal.state == ProposalStates.DRAFT:
        return True
    return part in unlocked_parts(proposal)


def require_applicant_may_edit(proposal, part):
    """Refuse a change of ``part`` the applicant may not make now."""
    if not applicant_may_edit(proposal, part):
        raise IncorrectStateException(LOCKED_MESSAGE)


def run_unlockable_parts(run):
    """The parts a request for changes at a step of the post-award ``run``
    may unlock: the answers of the run's applicant step, and what an
    extension asks for. Never the accepted proposal's own content."""
    parts = [ChangeRequestParts.RUN_FORM]
    if run.phase == WorkflowPhases.EXTENSION:
        parts.append(ChangeRequestParts.RUN_REQUEST)
    return parts


def run_part_unlocked(run, part) -> bool:
    """Whether the open request for changes unlocks ``part`` of ``run``."""
    return any(
        request.step_instance.run_id == run.pk
        and part in (request.unlocked_parts or [])
        for request in open_applicant_requests(run.proposal)
    )


def unlockable_parts(proposal, instance=None):
    """The parts a request for changes on ``proposal`` may unlock.

    The Project details fields the call collects -- its name, the duration
    when the call offers a choice of one, and the configurable fields it
    does not hide -- and the documents, requested resources, team and, when
    the call has one, compliance checklist answers. At a step of a
    post-award run (``instance``), the run's parts instead
    (``run_unlockable_parts``).
    """
    if instance is not None and instance.run_id:
        return run_unlockable_parts(instance.run)
    call = proposal.round.call
    states = models.CallProposalFieldConfig.get_states_for_call(call)
    parts = []
    for name in ChangeRequestParts.FIELDS:
        if states.get(name) == ProposalFieldStates.HIDDEN:
            continue
        parts.append(ChangeRequestParts.field(name))
    if call.allowed_duration_months:
        parts.append(ChangeRequestParts.field(ChangeRequestParts.DURATION_FIELD))
    if states.get("supporting_documentation") != ProposalFieldStates.HIDDEN:
        parts.append(ChangeRequestParts.DOCUMENTS)
    parts += [ChangeRequestParts.RESOURCES, ChangeRequestParts.TEAM]
    if call.compliance_checklist_id:
        parts.append(ChangeRequestParts.COMPLIANCE)
    return parts


def close(change_request, resolution, user=None):
    change_request.closed_at = timezone.now()
    change_request.closed_by = user
    change_request.resolution = resolution
    change_request.save(
        update_fields=["closed_at", "closed_by", "resolution", "modified"]
    )
    return change_request


def close_open(queryset, resolution, user=None):
    """Close the open requests of ``queryset`` at once, as ``resolution``."""
    now = timezone.now()
    return queryset.filter(closed_at__isnull=True).update(
        closed_at=now, closed_by=user, resolution=resolution, modified=now
    )


def close_open_of_proposal(proposal, resolution, user=None):
    return close_open(
        models.ProposalChangeRequest.objects.filter(proposal=proposal),
        resolution,
        user,
    )


def close_open_of_step(instance, resolution, user=None):
    return close_open(
        models.ProposalChangeRequest.objects.filter(step_instance=instance),
        resolution,
        user,
    )


OFFERINGS_FIXED_MESSAGE = _(
    "The offerings requested are being assessed: their amounts and "
    "attributes may change, but no offering can be added or removed."
)


def has_offering_slots(proposal) -> bool:
    """Whether a per-offering step has opened its slots on ``proposal``:
    those are keyed by the offerings requested, which then stay as they are."""
    return models.StepContribution.objects.filter(
        proposal_step__proposal=proposal, requested_offering__isnull=False
    ).exists()


def close_open_of_contributions(contributions, resolution, user=None):
    """Close the open reopenings of ``contributions`` (a queryset or list of
    contributions), as ``resolution``."""
    return close_open(
        models.ProposalChangeRequest.objects.filter(contribution__in=contributions),
        resolution,
        user,
    )
