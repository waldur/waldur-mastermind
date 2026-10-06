"""A round's lifecycle after its cut-off, driven by call managers.

``evaluating`` is entered automatically (``utils.start_round_evaluation``);
the transitions here are the call manager's round actions.
"""

import logging

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.logging.enums import EventType
from waldur_mastermind.proposal import models, tasks, utils, workflow_service
from waldur_mastermind.proposal.enums import (
    ProposalStates,
    ResultsPublication,
    RoundLifecycleStates,
    RoundStatuses,
    UndecidedAtRoundCompletion,
    WorkflowStepInstanceStatuses,
)

logger = logging.getLogger(__name__)

UNDER_EVALUATION = (ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW)


def _lock_ended_round(call_round):
    """Lock the round and start its lifecycle if the sweep has not yet."""
    call_round = (
        models.Round.objects.select_for_update()
        .select_related("call__manager__customer")
        .get(pk=call_round.pk)
    )
    if call_round.status != RoundStatuses.ENDED:
        raise exceptions.ValidationError(
            _("The round has not reached its cut-off yet.")
        )
    if call_round.lifecycle_state is None:
        utils.start_round_evaluation(call_round)
    return call_round


class UndecidedProposalsError(exceptions.ValidationError):
    """Publishing is refused: some proposals of the round have no decision."""

    default_code = "undecided_proposals"
    message = _(
        "%(count)s proposal(s) of this round have no decision yet. "
        "Decide them first, or publish with force and a reason."
    )

    def __init__(self, undecided_count):
        self.undecided_count = undecided_count
        super().__init__(self.message % {"count": undecided_count})


class UndecidedAtCompletionError(UndecidedProposalsError):
    """Completing is refused: some proposals of the round have no decision."""

    message = _(
        "%(count)s proposal(s) of this round have no decision yet. Decide "
        "them before completing the round, or have the round reject them "
        "on completion."
    )


class HeldDecisionsAtCompletionError(exceptions.ValidationError):
    """Completing is refused: a decision of the round was never announced.

    After publication a decision is held only when its release failed, so
    its applicant has not been told and the proposal is not finally decided.
    """

    default_code = "held_decisions"

    def __init__(self, held_decisions_count):
        self.held_decisions_count = held_decisions_count
        super().__init__(
            _(
                "%(count)s decision(s) of this round could not be carried out "
                "and have not been announced. Publish the results again to "
                "retry them before completing the round."
            )
            % {"count": held_decisions_count}
        )


def _refuse_unless(call_round, *allowed):
    if call_round.lifecycle_state not in allowed:
        raise exceptions.ValidationError(
            _("The round is %(state)s, so this step does not apply.")
            % {"state": call_round.get_lifecycle_state_display()}
        )


def _decision_made():
    """Whether the proposal's allocation decision has been taken.

    A proposal still in review may be past it: an approval announced before
    the call started holding decisions leaves it awaiting the applicant's
    award response. A decision that lapsed is taken too; one reopened is not.
    """
    return Exists(
        models.ProposalWorkflowStepInstance.objects.filter(
            proposal=OuterRef("pk"),
            step=workflow_service.DECISION_STEP,
            status__in=(
                WorkflowStepInstanceStatuses.COMPLETED,
                WorkflowStepInstanceStatuses.EXPIRED,
            ),
        )
    )


@transaction.atomic
def start_deciding(call_round):
    """``evaluating`` → ``deciding``: the batch goes to the decision body."""
    call_round = _lock_ended_round(call_round)
    _refuse_unless(call_round, RoundLifecycleStates.EVALUATING)
    call_round.lifecycle_state = RoundLifecycleStates.DECIDING
    call_round.deciding_started_at = timezone.now()
    call_round.save(update_fields=["lifecycle_state", "deciding_started_at"])
    utils.emit_round_event(
        call_round,
        "Decision on {round_name} of call {call_name} has started.",
        EventType.ROUND_DECISION_STARTED,
    )
    return call_round


@transaction.atomic
def publish_results(call_round, user, force=False, reason=""):
    """Publish every decision of the round at once.

    Refused while a proposal of the round is still being evaluated without a
    decision, unless forced with a reason; those proposals carry on, and
    their decisions are announced as they are made. A call that announces
    decisions as they are made holds nothing back, so there it only records
    the lifecycle. Each held decision is then carried out as it would have
    been when it was made, and the applicant hears about it through the same
    notification.

    Each decision is released on its own: one that fails (an allocation that
    cannot be provisioned, say) is rolled back alone and stays held, the rest
    are published. Publishing again releases what is still held. Returns the
    round and the proposals whose release failed.
    """
    # Proposals before the round, in primary key order: the order a decision
    # takes them in (workflow_service._decision_is_withheld), so the two
    # cannot deadlock.
    proposals = list(
        models.Proposal.objects.select_for_update(of=("self",))
        .filter(round=call_round, state__in=UNDER_EVALUATION)
        .select_related("round__call")
        .annotate(decision_made=_decision_made())
        .order_by("pk")
    )
    call_round = _lock_ended_round(call_round)
    held = [proposal for proposal in proposals if proposal.decision_held]
    republishing = (
        call_round.lifecycle_state == RoundLifecycleStates.RESULTS_PUBLISHED
        and bool(held)
    )
    if not republishing:
        _refuse_unless(
            call_round,
            RoundLifecycleStates.EVALUATING,
            RoundLifecycleStates.DECIDING,
        )

    undecided = 0
    if call_round.call.publish_results == ResultsPublication.WITH_ROUND:
        undecided = sum(
            1
            for proposal in proposals
            if not (proposal.decision_held or proposal.decision_made)
        )
    if undecided and not force and not republishing:
        raise UndecidedProposalsError(undecided)

    decided = []
    failed = []
    for proposal in held:
        try:
            with transaction.atomic():
                final_state = workflow_service.release_held_decision(proposal)
        except Exception:
            logger.exception(
                "Failed to release the held decision of proposal %s",
                proposal.uuid,
            )
            failed.append(proposal)
            continue
        if final_state is not None:
            decided.append((proposal.uuid, final_state))

    if not republishing:
        call_round.lifecycle_state = RoundLifecycleStates.RESULTS_PUBLISHED
        call_round.results_published_at = timezone.now()
        call_round.results_published_by = user
        call_round.results_forced_reason = reason if undecided else ""
        call_round.save(
            update_fields=[
                "lifecycle_state",
                "results_published_at",
                "results_published_by",
                "results_forced_reason",
            ]
        )
        utils.emit_round_event(
            call_round,
            "Results of {round_name} of call {call_name} have been published.",
            EventType.ROUND_RESULTS_PUBLISHED,
        )

    # Same notifications a decision made on the spot sends: the applicant's
    # outcome mail and the reviewers'. An award continuing to the award
    # response is announced by that step starting instead.
    for proposal_uuid, final_state in decided:
        transaction.on_commit(
            lambda proposal_uuid=proposal_uuid, final_state=final_state: (
                tasks.notify_proposal_decision(
                    proposal_uuid, ProposalStates.IN_REVIEW, final_state
                )
            )
        )
    return call_round, failed


@transaction.atomic
def complete(call_round, user):
    """``results_published`` → ``closed``: nothing more happens in the round.

    Refused while a decision of the round is still held: its release failed
    at publication and publishing again retries it. Every proposal of the
    round must be decided first. What happens to one
    still without a decision is the round's rule, or else its call's:
    ``refuse`` refuses to complete, ``reject`` rejects each at the step it
    stands in -- a final outcome, announced at once -- and completes.

    Each rejection is made on its own: one that fails is rolled back alone
    and listed, the rest stand, and the round stays open until completing
    again rejects what is left. Returns the round, the proposals rejected
    and the proposals whose rejection failed.
    """
    # Proposals before the round, in primary key order, as publishing takes
    # them (and every decision, see workflow_service._decision_is_withheld).
    proposals = list(
        models.Proposal.objects.select_for_update(of=("self",))
        .filter(round=call_round, state__in=UNDER_EVALUATION)
        .select_related("round__call__manager__customer")
        .annotate(decision_made=_decision_made())
        .order_by("pk")
    )
    call_round = _lock_ended_round(call_round)
    _refuse_unless(call_round, RoundLifecycleStates.RESULTS_PUBLISHED)

    # Held after publication means its release failed: not final until it
    # is announced, whatever the rule for undecided proposals says.
    held = sum(1 for proposal in proposals if proposal.decision_held)
    if held:
        raise HeldDecisionsAtCompletionError(held)

    undecided = [
        proposal
        for proposal in proposals
        if not (proposal.decision_held or proposal.decision_made)
    ]
    if (
        undecided
        and call_round.undecided_at_completion_rule == UndecidedAtRoundCompletion.REFUSE
    ):
        raise UndecidedAtCompletionError(len(undecided))

    rejected = []
    failed = []
    for proposal in undecided:
        try:
            with transaction.atomic():
                previous_state = workflow_service.reject_undecided(proposal, user)
        except Exception:
            logger.exception(
                "Failed to reject undecided proposal %s on round completion",
                proposal.uuid,
            )
            failed.append(proposal)
            continue
        rejected.append(proposal)
        # The results are final once the round completes: the applicant
        # hears at once, as for any other rejection announced.
        transaction.on_commit(
            lambda proposal_uuid=proposal.uuid, previous_state=previous_state: (
                tasks.notify_proposal_decision(
                    proposal_uuid, previous_state, ProposalStates.REJECTED
                )
            )
        )

    if rejected:
        utils.emit_round_event(
            call_round,
            "{rejected_count} undecided proposal(s) of {round_name} of call "
            "{call_name} were rejected by {actor} on completing the round.",
            EventType.ROUND_UNDECIDED_PROPOSALS_REJECTED,
            rejected_count=len(rejected),
            actor=user.full_name or user.username,
            actor_uuid=user.uuid.hex,
        )
    if failed:
        return call_round, rejected, failed

    call_round.lifecycle_state = RoundLifecycleStates.CLOSED
    call_round.closed_at = timezone.now()
    call_round.save(update_fields=["lifecycle_state", "closed_at"])
    utils.emit_round_event(
        call_round,
        "{round_name} of call {call_name} has been closed.",
        EventType.ROUND_CLOSED,
    )
    return call_round, rejected, failed
