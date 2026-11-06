"""A round's lifecycle after its cut-off, driven by call managers.

``evaluating`` is entered automatically (``utils.start_round_evaluation``);
the transitions here are the call manager's round actions.
"""

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.logging.enums import EventType
from waldur_mastermind.proposal import (
    budgets,
    models,
    proposal_roles,
    tasks,
    utils,
    workflow_service,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    OnDepletedBudget,
    ProposalStates,
    ResultsPublication,
    RoundLifecycleStates,
    RoundStatuses,
    UndecidedAtRoundCompletion,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
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
            is_allocation_decision=True,
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


ADOPTION_MISSING_MESSAGE = _(
    "The round's results must be adopted before they are published. Record "
    "the adoption first."
)


@transaction.atomic
def record_adoption(call_round, user, values):
    """Record who adopted the round's results, when, and with what document.

    A call that requires adoption takes it while the round is deciding: after
    its proposals have been evaluated and before the results are published,
    which waits for it. Otherwise it may be recorded any time after the
    cut-off, as before. The first recording is logged and the call's managers
    are told; correcting the record later is neither.
    """
    call_round = _lock_ended_round(call_round)
    if (
        call_round.call.adoption_required
        and call_round.lifecycle_state != RoundLifecycleStates.DECIDING
    ):
        raise exceptions.ValidationError(
            _(
                "The round is %(state)s. This call takes the adoption while "
                "the round is deciding."
            )
            % {"state": call_round.get_lifecycle_state_display()}
        )
    first = call_round.adopted_at is None
    for field, value in values.items():
        setattr(call_round, field, value)
    call_round.save(update_fields=list(values))
    if first and call_round.adopted_at is not None:
        utils.emit_round_event(
            call_round,
            "Results of {round_name} of call {call_name} were adopted on {adopted_at}.",
            EventType.ROUND_RESULTS_ADOPTED,
            adopted_at=call_round.adopted_at.isoformat(),
        )
        transaction.on_commit(
            lambda: tasks.notify_call_managers_about_round_adoption.delay(
                call_round.uuid.hex, user.uuid.hex
            )
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
        # Not even forced: force answers for undecided proposals, never for a
        # list the board has not adopted.
        if call_round.call.adoption_required and call_round.adopted_at is None:
            raise exceptions.ValidationError(ADOPTION_MISSING_MESSAGE)

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


def _failure_detail(error):
    """The reason a proposal's step could not be completed, as one line."""
    if isinstance(error, exceptions.ValidationError):
        return " ".join(_flatten_detail(error.detail))
    if isinstance(error, DjangoValidationError):
        return " ".join(str(message) for message in error.messages)
    if isinstance(error, ValueError):
        return str(error)
    return str(_("The step could not be completed; the error has been logged."))


def _flatten_detail(detail):
    if isinstance(detail, dict):
        for value in detail.values():
            yield from _flatten_detail(value)
    elif isinstance(detail, (list, tuple)):
        for value in detail:
            yield from _flatten_detail(value)
    else:
        yield str(detail)


def _complete_step_for_proposal(
    proposal_id, call_step, outcome, feedback, user, unless_award_fits=False
):
    """Complete ``call_step`` for one proposal; the caller holds a transaction.

    Returns ``(completed, entry)`` where ``completed`` is False for a proposal
    that was skipped. Raises whatever stops the step from being completed.
    With ``unless_award_fits`` -- declining it for want of budget -- the
    proposal is skipped instead when its award fits what remains of the
    budgets now, checked under their lock.
    """
    # The proposal before its step, then (in complete_step) the round: the
    # order every decision takes.
    proposal = (
        models.Proposal.objects.select_for_update(of=("self",))
        .select_related("round__call__manager__customer")
        .get(pk=proposal_id)
    )
    entry = {"proposal_uuid": proposal.uuid, "proposal_name": proposal.name}
    instance = (
        proposal.workflow_step_instances.select_for_update(of=("self",))
        .select_related("call_step")
        .filter(
            call_step=call_step,
            status=WorkflowStepInstanceStatuses.ACTIVE,
        )
        .first()
        if proposal.state == ProposalStates.IN_REVIEW
        else None
    )
    if instance is None:
        return False, {
            **entry,
            "detail": _("Not at this step."),
            "proposal_state": proposal.state,
            "current_step_name": workflow_service.standing_label(proposal),
        }
    if not proposal_permissions.user_can_act_on_step(user, instance):
        return False, {
            **entry,
            "detail": _("You cannot act on this step of the proposal."),
            "proposal_state": proposal.state,
            "current_step_name": call_step.name or call_step.key,
        }
    if unless_award_fits and not budgets.exceeded(
        proposal, budgets.locked_pools(proposal.round)
    ):
        return False, {
            **entry,
            "detail": _(
                "Not declined: the award fits what remains of the budget now. "
                "Decide it again."
            ),
            "proposal_state": proposal.state,
            "current_step_name": call_step.name or call_step.key,
        }
    next_instance = workflow_service.complete_step(
        proposal=proposal,
        current_instance=instance,
        outcome=outcome,
        outcome_reason=feedback.get(proposal.uuid.hex, ""),
        completed_by=user,
    )
    proposal.refresh_from_db()
    if (
        next_instance is None
        and not proposal.decision_held
        and proposal.state
        in (
            ProposalStates.ACCEPTED,
            ProposalStates.REJECTED,
            ProposalStates.CANCELED,
        )
    ):
        transaction.on_commit(
            lambda proposal_uuid=proposal.uuid, state=proposal.state: (
                tasks.notify_proposal_decision(
                    proposal_uuid, ProposalStates.IN_REVIEW, state
                )
            )
        )
    return True, {
        **entry,
        "detail": (
            _(
                "Completed; the decision is held until the round's results "
                "are published."
            )
            if proposal.decision_held
            else _("Completed.")
        ),
        "proposal_state": proposal.state,
        "current_step_name": workflow_service.standing_label(proposal),
    }


def complete_step_for_round(call_round, call_step, outcome, feedback, user):
    """Complete ``call_step`` with ``outcome`` for every proposal of the round at it.

    For a step such as the committee meeting or the ranking, which the round's
    proposals reach one by one and leave together. Each proposal goes through
    ``workflow_service.complete_step`` on its own, in its own transaction and
    under its own row locks, taken proposal first, then round: the step's
    gates, notifications and ledger apply as when the step is completed on the
    proposal. The caller must not hold a transaction, or the proposals would
    no longer commit one by one. A proposal that cannot be completed, for
    whatever reason, is rolled back alone and listed in ``failed``; the rest
    of the round goes on. ``feedback`` maps a proposal's uuid hex to the text
    stored as its outcome reason.

    Proposals not at the step -- not reached yet, past it, or out of
    evaluation -- are skipped, so running the action again changes nothing.
    Returns ``(completed, skipped, failed)``, each a list of dicts.
    """
    completed, skipped, failed = [], [], []
    proposals = list(
        models.Proposal.objects.filter(round=call_round)
        .order_by("pk")
        .values_list("pk", "uuid", "name", "state")
    )
    for proposal_id, proposal_uuid, proposal_name, proposal_state in proposals:
        try:
            with transaction.atomic():
                done, entry = _complete_step_for_proposal(
                    proposal_id, call_step, outcome, feedback, user
                )
        except Exception as error:
            if not isinstance(
                error, (exceptions.ValidationError, DjangoValidationError, ValueError)
            ):
                logger.exception(
                    "Step %s could not be completed for proposal %s",
                    call_step.uuid.hex,
                    proposal_uuid.hex,
                )
            failed.append(
                {
                    "proposal_uuid": proposal_uuid,
                    "proposal_name": proposal_name,
                    "detail": _failure_detail(error),
                    "proposal_state": proposal_state,
                    "current_step_name": call_step.name or call_step.key,
                }
            )
            continue
        (completed if done else skipped).append(entry)
    logger.info(
        "Step %s completed as %s for round %s by user %s: %s completed, "
        "%s skipped, %s failed",
        call_step.uuid.hex,
        outcome,
        call_round.uuid.hex,
        user.uuid.hex,
        len(completed),
        len(skipped),
        len(failed),
    )
    return completed, skipped, failed


# -- first come, first served ------------------------------------------------------

QUALITY_THRESHOLD_REASON = _("Below the quality threshold.")
DEPLETED_REASON = _("The resources of this cut-off are depleted.")


class NotReadyForAllocationError(exceptions.ValidationError):
    """Allocating is refused: some proposals have not reached the decision."""

    default_code = "not_ready_for_allocation"

    def __init__(self, waiting_count):
        self.waiting_count = waiting_count
        super().__init__(
            _(
                "%(count)s proposal(s) of this round have not reached the "
                "allocation decision yet; an earlier submission must not be "
                "overtaken. Wait for them, or allocate with force and a reason."
            )
            % {"count": waiting_count}
        )


def _at_decision():
    return Exists(
        models.ProposalWorkflowStepInstance.objects.filter(
            proposal=OuterRef("pk"),
            is_allocation_decision=True,
            status=WorkflowStepInstanceStatuses.ACTIVE,
        )
    )


def _waiting_for_decision(call_round):
    """Proposals of the round still being evaluated before the decision."""
    return (
        models.Proposal.objects.filter(round=call_round, state__in=UNDER_EVALUATION)
        .annotate(decision_made=_decision_made(), at_decision=_at_decision())
        .filter(decision_made=False, at_decision=False)
        .count()
    )


def _why_not_decided_by(user, proposal, instance):
    """Why ``user`` may not decide the proposal in a batch, or None.

    Nobody, staff included, decides a proposal they apply with or have a
    conflict of interest on record with.
    """
    if proposal_roles.is_on_applicant_team(user, proposal):
        return _("You are on the applicant team of this proposal.")
    if models.ConflictOfInterest.objects.filter(
        reviewer__user=user,
        proposal=proposal,
        status__in=proposal_roles.BLOCKING_COI_STATUSES,
    ).exists():
        return _("You have a conflict of interest with this proposal.")
    if not proposal_permissions.user_can_act_on_step(user, instance):
        return _("You cannot act on this step of the proposal.")
    return None


def plan_submission_order(call_round, user, on_depleted=OnDepletedBudget.DECLINE):
    """Plan the allocation decision of every proposal of the round at it, in
    the order the proposals were submitted.

    Each proposal below its decision's quality threshold is declined. The
    others are approved while their award fits what remains of every pool
    it draws on -- the call-wide one and the round's -- the remainder going
    down with each approval; one that does not fit is declined (or left,
    ``on_depleted=leave``), and a later, smaller one still gets its chance.
    Writes nothing. Returns ``(plan, budget_rows)``: a row per proposal, and
    per pool its remaining amount before and after.
    """
    pools = list(budgets.pools_for_round(call_round))
    remaining = {
        pool.pk: pool.offered_total - budgets.awarded_so_far(pool) for pool in pools
    }
    before = dict(remaining)
    candidates = (
        models.Proposal.objects.filter(round=call_round, state=ProposalStates.IN_REVIEW)
        .annotate(at_decision=_at_decision())
        .filter(at_decision=True)
        .order_by(F("submitted_at").asc(nulls_last=True), "pk")
    )
    offering_names = {
        ro.pk: ro.offering.name
        for ro in call_round.call.requestedoffering_set.select_related("offering")
    }
    plan = []
    for order, proposal in enumerate(candidates, start=1):
        instance = (
            proposal.workflow_step_instances.select_related("call_step")
            .filter(
                is_allocation_decision=True,
                status=WorkflowStepInstanceStatuses.ACTIVE,
            )
            .first()
        )
        amounts = budgets.award_amounts(proposal)
        gate = workflow_service.score_gate(instance)
        entry = {
            "order": order,
            "proposal_uuid": proposal.uuid,
            "proposal_name": proposal.name,
            "proposal_slug": proposal.slug or "",
            "submitted_at": proposal.submitted_at,
            "score": gate["score"] if gate else None,
            "threshold": float(gate["threshold"]) if gate else None,
            "award": [
                {
                    "offering_name": offering_names.get(ro_id, ""),
                    "component_type": component_type,
                    "amount": amount,
                }
                for (ro_id, component_type), amount in sorted(
                    amounts.items(), key=lambda item: (item[0][0], item[0][1])
                )
            ],
            "proposal_id": proposal.pk,
            "call_step": instance.call_step,
        }
        refusal = _why_not_decided_by(user, proposal, instance)
        if refusal:
            plan.append({**entry, "planned_outcome": "skipped", "reason": refusal})
            continue
        if gate is not None and not gate["passes"]:
            plan.append(
                {
                    **entry,
                    "planned_outcome": "declined",
                    "reason": str(QUALITY_THRESHOLD_REASON),
                }
            )
            continue
        try:
            workflow_service.check_approval_gates(proposal, instance)
        except ValueError as error:
            plan.append({**entry, "planned_outcome": "left", "reason": str(error)})
            continue
        draws = [
            (pool, amount)
            for pool in pools
            if (
                amount := amounts.get(
                    (
                        pool.offering_component.requested_offering_id,
                        pool.offering_component.component_type,
                    )
                )
            )
        ]
        fits = all(amount <= remaining[pool.pk] for pool, amount in draws)
        if fits:
            for pool, amount in draws:
                remaining[pool.pk] -= amount
            plan.append({**entry, "planned_outcome": "approved", "reason": ""})
        elif on_depleted == OnDepletedBudget.DECLINE:
            plan.append(
                {**entry, "planned_outcome": "declined", "reason": str(DEPLETED_REASON)}
            )
        else:
            plan.append(
                {**entry, "planned_outcome": "left", "reason": str(DEPLETED_REASON)}
            )
    budget_rows = [
        {
            "pool": pool,
            "offering_name": pool.offering_component.requested_offering.offering.name,
            "component_type": pool.offering_component.component_type,
            "round": pool.round,
            "offered_total": pool.offered_total,
            "remaining_before": before[pool.pk],
            "remaining_after": remaining[pool.pk],
        }
        for pool in pools
    ]
    return plan, budget_rows


def allocate_in_submission_order(
    call_round,
    user,
    *,
    preview=True,
    on_depleted=OnDepletedBudget.DECLINE,
    force=False,
    reason="",
):
    """Decide the round's allocation decisions first come, first served.

    Refused before the round's cut-off, once it is closed, and while a
    proposal of it is still being evaluated before the decision -- an earlier
    submission must not be overtaken -- unless forced with a reason. The
    plan is ``plan_submission_order``'s; with ``preview`` it is only
    returned. Otherwise each planned decision is taken on its own, in its own
    transaction, as if taken on the proposal: its gates, the budget check
    under the pools' lock, held decisions and notifications apply. A
    decision that fails -- a manual approval meanwhile used up the budget,
    say -- is rolled back alone and listed in ``failed``; the rest go on.
    The caller must not hold a transaction.
    """
    if call_round.status != RoundStatuses.ENDED:
        raise exceptions.ValidationError(
            _("The round has not reached its cut-off yet.")
        )
    if call_round.lifecycle_state == RoundLifecycleStates.CLOSED:
        raise exceptions.ValidationError(_("The round is closed."))
    waiting = _waiting_for_decision(call_round)
    if waiting and not force:
        raise NotReadyForAllocationError(waiting)

    plan, budget_rows = plan_submission_order(call_round, user, on_depleted)
    result = {
        "preview": preview,
        "has_budget": bool(budget_rows),
        "plan": plan,
        "budgets": budget_rows,
        "completed": [],
        "skipped": [],
        "failed": [],
    }
    if preview:
        return result

    with transaction.atomic():
        _lock_ended_round(call_round)
    for entry in plan:
        summary = {
            "proposal_uuid": entry["proposal_uuid"],
            "proposal_name": entry["proposal_name"],
        }
        outcome = {
            "approved": WorkflowStepOutcomes.APPROVED,
            "declined": WorkflowStepOutcomes.DECLINED,
        }.get(entry["planned_outcome"])
        if outcome is None:
            result["skipped"].append({**summary, "detail": entry["reason"]})
            continue
        call_step = entry["call_step"]
        feedback = (
            {entry["proposal_uuid"].hex: entry["reason"]} if entry["reason"] else {}
        )
        try:
            with transaction.atomic():
                done, outcome_entry = _complete_step_for_proposal(
                    entry["proposal_id"],
                    call_step,
                    outcome,
                    feedback,
                    user,
                    # Planned before any lock: a budget freed since must not
                    # decline it.
                    unless_award_fits=entry["reason"] == str(DEPLETED_REASON),
                )
        except Exception as error:
            if not isinstance(
                error, (exceptions.ValidationError, DjangoValidationError, ValueError)
            ):
                logger.exception(
                    "Allocation decision could not be taken for proposal %s",
                    entry["proposal_uuid"].hex,
                )
            result["failed"].append(
                {
                    **summary,
                    "detail": _failure_detail(error),
                    "proposal_state": ProposalStates.IN_REVIEW,
                    "current_step_name": call_step.name or call_step.key,
                }
            )
            continue
        (result["completed"] if done else result["skipped"]).append(outcome_entry)

    utils.emit_round_event(
        call_round,
        "{actor} allocated {round_name} of call {call_name} in submission "
        "order: {completed_count} decided, {skipped_count} skipped, "
        "{failed_count} failed.",
        EventType.ROUND_ALLOCATED_IN_SUBMISSION_ORDER,
        actor=user.full_name or user.username,
        actor_uuid=user.uuid.hex,
        completed_count=len(result["completed"]),
        skipped_count=len(result["skipped"]),
        failed_count=len(result["failed"]),
        forced=bool(waiting and force),
        forced_reason=reason if waiting and force else "",
    )
    return result
