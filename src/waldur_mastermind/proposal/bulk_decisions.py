"""One step of a call decided for many proposals at once.

A call manager takes a plain decision -- the administrative check, say -- for
a selection of the call's proposals with one outcome and one reason. Each
proposal is decided as ``complete_workflow_step`` decides it on its page: the
same standing on the step, the same permission, the same gates, through
``workflow_service.complete_step``, each in its own transaction under its own
row locks, so one refusal does not undo the others. What happens to each
comes back as ``decided``, or ``refused`` with the reason.

Who may start one: the call's managers and organisers who decide its
proposals, staff, and the completer of the step on the call
(``permissions.may_decide_in_bulk``) -- and only for a selection holding at
least one proposal standing on the step that they may act on; otherwise
nothing is recorded. Of a proposal the user may not act on, and does not
oversee, only its id and the reason are told.

A preview (``dry_run``) checks the selection the same way and writes nothing:
the checks it makes are the ones ``complete_step`` makes before it writes
(``workflow_service.enforce_completion_gates``), so it refuses exactly what
the real run would, as long as nothing changes in between.

A selection of up to ``BACKGROUND_THRESHOLD`` proposals is decided, or
previewed, within the request. A larger one is recorded
(``models.BulkStepDecision``) and run by a background task, which records
each proposal in the transaction that decides it, under the run's row lock:
a second executor of the same run waits, then goes on from the first
proposal not yet recorded. The task is acknowledged late, so a worker lost
mid-run hands it back to the broker, and ``resume_stale`` dispatches again a
run that has stopped moving, up to ``MAX_ATTEMPTS`` times. A real run is
recorded whatever its size, with one event on the call.

Only plain decision steps are decided in bulk: the allocation decision
settles each award and its budget, an assessment waits for its contributors,
a step run once per round is completed for the round, and an applicant action
is the applicant's. Those stay on each proposal's page.
"""

import dataclasses
import logging
from datetime import timedelta
from functools import cached_property

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.text import Truncator
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.core.models import User
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.utils import get_scope_ids
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.proposal import models, proposal_roles, tasks, workflow_service
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    BulkStepDecisionResults,
    BulkStepDecisionStates,
    ProposalStates,
    ResponsibleRoles,
    StepKinds,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.managers import get_connected_calls

logger = logging.getLogger(__name__)

# A selection up to this size is decided, or previewed, within the request;
# a larger one by a background task.
BACKGROUND_THRESHOLD = 25
# The most proposals one run takes.
MAX_PROPOSALS = 2000
# A run in the background that has recorded nothing for this long has
# stopped moving: the sweeper dispatches it again.
STALE_AFTER = timedelta(minutes=10)
# How often the sweeper dispatches again a run that a worker had claimed,
# before giving it up.
MAX_ATTEMPTS = 3
# A run no worker has claimed for this long is given up.
PENDING_GIVE_UP = timedelta(days=1)
# A preview run in the background is kept this long: it is read by the
# dialog that asked for it, and then no more.
PREVIEW_KEPT = timedelta(days=1)

NOT_FOUND = _("Not found in this call.")
NOT_IN_WORKFLOW = _("Not under evaluation.")
NOT_AT_STEP = _("Not at this step.")
MAY_NOT_ACT = _("You cannot act on this step of the proposal.")
CAN_BE_DECIDED = _("Can be decided.")
UNEXPECTED = _("The step could not be decided; the error has been logged.")
RUN_FAILED = _("The run stopped on an unexpected error; it has been logged.")
USER_GONE = _("The user who started the run may no longer decide this step.")
GAVE_UP = _("The run was interrupted too often and has been given up.")

FINISHED = (BulkStepDecisionStates.DONE, BulkStepDecisionStates.FAILED)
UNFINISHED = (BulkStepDecisionStates.PENDING, BulkStepDecisionStates.RUNNING)


class Refused(Exception):
    """A proposal whose step is not decided, and why. ``proposal`` is set
    when its details -- name, state, where it stands -- may be told."""

    def __init__(self, detail, proposal=None):
        super().__init__(str(detail))
        self.detail = str(detail)
        self.proposal = proposal


def bulk_refusal(call_step):
    """Why ``call_step`` is not decided for several proposals at once, or
    None when it is."""
    if call_step.kind != StepKinds.DECISION:
        return _(
            "Only a decision step is decided for several proposals at once: "
            "an assessment waits for its contributors, an assignment for its "
            "holders, and an applicant action is the applicant's."
        )
    if call_step.is_allocation_decision:
        return _(
            "The allocation decision settles each award and its budget: it is "
            "decided on each proposal."
        )
    if call_step.round_scoped:
        return _("This step is completed once per round: complete it for the round.")
    if call_step.completer == ResponsibleRoles.APPLICANT:
        return _("A step the applicant completes is theirs to decide.")
    return None


def _step_refusal(run):
    """Why the run can no longer decide its step -- the step was removed,
    changed kind, or no longer has the run's outcome -- or None."""
    call_step = run.call_step
    if call_step is None or call_step.call_id != run.call_id:
        return _("The step no longer exists.")
    refusal = bulk_refusal(call_step)
    if refusal:
        return refusal
    if run.outcome not in call_step.allowed_outcomes:
        return _("The step no longer has the outcome %(outcome)s.") % {
            "outcome": run.outcome
        }
    return None


@dataclasses.dataclass
class Decision:
    """What one run decides, and who decides it."""

    call: models.Call
    call_step: models.CallWorkflowStep | None
    outcome: str
    outcome_reason: str
    user: User
    # Whether the user acts with staff access (``has_staff_access``).
    staff: bool

    @cached_property
    def oversees(self):
        """Whether the user drives the call's proposals through its workflow,
        or acts as staff: they act on every proposal of the step but those
        they apply with."""
        return self.staff or proposal_permissions.manages_call_workflow(
            self.user, self.call
        )

    @cached_property
    def team_proposal_ids(self):
        return set(proposal_roles.applicant_team_proposal_ids(self.user))

    def oversees_proposal(self, proposal):
        return self.oversees and proposal.pk not in self.team_proposal_ids

    def may_act(self, instance):
        """Whether the user may decide ``instance``, as
        ``complete_workflow_step`` checks it under the proposal's lock."""
        if not self.user.is_active:
            return False
        if self.user.is_staff:
            return True
        if self.oversees_proposal(instance.proposal):
            # As user_can_act_on_step decides it for the call's managers,
            # without its queries for each proposal.
            return True
        return proposal_permissions.user_may_act_on_locked_step(self.user, instance)


def unique_uuids(proposal_uuids):
    """The uuids as hex, each once, in the order named."""
    seen = {}
    for proposal_uuid in proposal_uuids:
        key = getattr(proposal_uuid, "hex", None) or str(proposal_uuid).replace("-", "")
        seen.setdefault(key, None)
    return list(seen)


def _visible(decision):
    return filter_queryset_for_user(
        models.Proposal.objects.filter(round__call=decision.call), decision.user
    )


def _active_instances(proposals, **filters):
    return (
        models.ProposalWorkflowStepInstance.objects.filter(
            proposal__in=proposals,
            status=WorkflowStepInstanceStatuses.ACTIVE,
            is_allocation_decision=False,
            **filters,
        )
        .select_related("call_step", "proposal__round__call")
        .order_by("call_step__order", "proposal_id")
    )


def acts_on_any(decision, proposal_uuids):
    """Whether the user may act on any of the named proposals standing on
    the step, and whether any of them stands on it at all."""
    proposals = _visible(decision).filter(uuid__in=unique_uuids(proposal_uuids))
    at_step = False
    for instance in _active_instances(proposals, call_step=decision.call_step):
        if not workflow_service.in_workflow(instance.proposal):
            continue
        at_step = True
        if decision.may_act(instance):
            return True, True
    return False, at_step


def decidable_steps(call, user, proposal_uuids, staff=False):
    """The steps the selected proposals stand on that the user decides in
    bulk, each with its outcomes and the proposals standing on it that the
    user may act on, largest group first. A proposal running steps side by
    side may stand in two groups."""
    decision = Decision(
        call=call, call_step=None, outcome="", outcome_reason="", user=user, staff=staff
    )
    proposals = _visible(decision).filter(uuid__in=unique_uuids(proposal_uuids))
    instances = _active_instances(
        proposals,
        call_step__kind=StepKinds.DECISION,
        call_step__is_allocation_decision=False,
        call_step__round_scoped=False,
    )
    groups = {}
    for instance in instances:
        if not workflow_service.in_workflow(instance.proposal):
            continue
        if not decision.may_act(instance):
            continue
        call_step = instance.call_step
        group = groups.setdefault(
            call_step.pk,
            {
                "call_step_uuid": call_step.uuid,
                "step_name": call_step.name or call_step.key,
                "allowed_outcomes": list(call_step.allowed_outcomes),
                "proposal_uuids": [],
            },
        )
        group["proposal_uuids"].append(instance.proposal.uuid)
    return sorted(
        groups.values(), key=lambda group: len(group["proposal_uuids"]), reverse=True
    )


def _fit(value, length):
    """``value`` cut to a result column of ``length`` characters."""
    return Truncator(value or "").chars(length)


def _entry(proposal_uuid, status, detail, proposal=None):
    """One proposal's result. ``proposal`` is given only for one the user
    may act on or oversees: of any other, only its id and why are told.
    Each value fits its column: where a proposal stands may name several
    steps running side by side."""
    return {
        "proposal_uuid": proposal_uuid,
        "proposal_name": _fit(proposal.name if proposal else "", 255),
        "status": status,
        "detail": str(detail),
        "proposal_state": _fit(proposal.state if proposal else "", 30),
        "current_step_name": _fit(
            (workflow_service.standing_label(proposal) or "") if proposal else "",
            255,
        ),
    }


def _refusal_entry(proposal_uuid, error):
    if isinstance(error, Refused):
        return _entry(
            proposal_uuid,
            BulkStepDecisionResults.REFUSED,
            error.detail,
            error.proposal,
        )
    if isinstance(error, exceptions.ValidationError):
        detail = error.detail
        if isinstance(detail, dict):
            detail = list(detail.values())
        if not isinstance(detail, list):
            detail = [detail]
        text = " ".join(str(item) for item in _flatten(detail))
    elif isinstance(error, DjangoValidationError):
        text = " ".join(str(message) for message in error.messages)
    elif isinstance(error, ValueError):
        text = str(error)
    elif isinstance(error, exceptions.APIException):
        # A refusal the engine words itself: an archived call, say.
        text = str(error.detail)
    else:
        text = str(UNEXPECTED)
    return _entry(proposal_uuid, BulkStepDecisionResults.REFUSED, text)


def _flatten(items):
    for item in items:
        if isinstance(item, (list, tuple)):
            yield from _flatten(item)
        elif isinstance(item, dict):
            yield from _flatten(list(item.values()))
        else:
            yield item


def _log_unexpected(error, decision, proposal_uuid):
    if not isinstance(
        error,
        (
            Refused,
            exceptions.ValidationError,
            DjangoValidationError,
            ValueError,
            # A refusal the engine words itself: the call archived mid-run.
            exceptions.APIException,
        ),
    ):
        logger.exception(
            "Step %s could not be decided for proposal %s",
            decision.call_step.uuid.hex,
            proposal_uuid,
        )


def _proposal(decision, proposal_uuid, lock):
    """The named proposal of the call the user sees, or None. With ``lock``
    its row is locked: the proposal before its step, the order every
    decision takes."""
    proposals = _visible(decision).filter(uuid=proposal_uuid)
    if lock:
        proposals = models.Proposal.objects.select_for_update(of=("self",)).filter(
            pk__in=proposals.values("pk")
        )
    return proposals.select_related("round__call").first()


def _check(decision, proposal, lock):
    """The proposal's active instance of the step, once everything that
    would refuse deciding it is checked; raises ``Refused`` otherwise.

    The checks of ``complete_workflow_step`` in its order: the proposal is in
    its workflow, stands on the step, the user may act on it, and its gates
    let the outcome through. With ``lock`` the step's row is locked: the
    caller holds the proposal's lock in a transaction.
    """
    shown = proposal if decision.oversees_proposal(proposal) else None
    if not workflow_service.in_workflow(proposal):
        raise Refused(NOT_IN_WORKFLOW, shown)
    instances = proposal.workflow_step_instances.select_related("call_step").filter(
        call_step=decision.call_step,
        status=WorkflowStepInstanceStatuses.ACTIVE,
    )
    if lock:
        instances = instances.select_for_update(of=("self",))
    instance = instances.first()
    if instance is None:
        raise Refused(NOT_AT_STEP, shown)
    if instance.is_allocation_decision:
        # Fixed on the proposal's path when it was submitted.
        raise Refused(bulk_refusal(decision.call_step) or NOT_AT_STEP, shown)
    if not decision.may_act(instance):
        raise Refused(MAY_NOT_ACT)
    reads_score = proposal_permissions.user_can_view_step_scores(
        decision.user, proposal, staff=decision.staff
    )
    try:
        if not reads_score:
            workflow_service.enforce_closed_scored_work(instance)
        workflow_service.enforce_completion_gates(proposal, instance, decision.outcome)
    except workflow_service.ScoreBelowThreshold as error:
        # A run's results are read by the call's managers as well as by
        # whoever started it, so the score stays out of them.
        raise Refused(error.concealed_message, proposal)
    except ValueError as error:
        raise Refused(str(error), proposal)
    return instance


def preview_one(decision, proposal_uuid):
    """What deciding the named proposal would do, writing nothing."""
    proposal = _proposal(decision, proposal_uuid, lock=False)
    if proposal is None:
        return _entry(proposal_uuid, BulkStepDecisionResults.REFUSED, NOT_FOUND)
    try:
        _check(decision, proposal, lock=False)
    except Exception as error:
        _log_unexpected(error, decision, proposal_uuid)
        return _refusal_entry(proposal_uuid, error)
    return _entry(
        proposal_uuid, BulkStepDecisionResults.DECIDED, CAN_BE_DECIDED, proposal
    )


def preview(decision, proposal_uuids):
    """What the run would do with each named proposal, in the order named,
    writing nothing."""
    return [
        preview_one(decision, proposal_uuid)
        for proposal_uuid in unique_uuids(proposal_uuids)
    ]


def _decided_detail(proposal):
    if proposal.decision_held:
        return _(
            "Decided; the decision is announced when the round's results are published."
        )
    return {
        ProposalStates.ACCEPTED: _("Decided. The proposal is accepted."),
        ProposalStates.REJECTED: _("Decided. The proposal is rejected."),
        ProposalStates.CANCELED: _("Decided. The proposal is canceled."),
    }.get(proposal.state, _("Decided."))


def _decide_locked(decision, proposal_uuid):
    """Decide the named proposal's step; the caller holds a transaction.
    Returns its result; raises whatever refuses it."""
    proposal = _proposal(decision, proposal_uuid, lock=True)
    if proposal is None:
        raise Refused(NOT_FOUND)
    instance = _check(decision, proposal, lock=True)
    in_run = instance.run_id is not None
    next_instance = workflow_service.complete_step(
        proposal=proposal,
        current_instance=instance,
        outcome=decision.outcome,
        outcome_reason=decision.outcome_reason,
        completed_by=decision.user,
    )
    proposal.refresh_from_db()
    if (
        next_instance is None
        and not in_run
        and not proposal.decision_held
        and proposal.state
        in (ProposalStates.ACCEPTED, ProposalStates.REJECTED, ProposalStates.CANCELED)
    ):
        # The workflow ended: tell the applicant, as on the proposal's page.
        # A failure to queue the mail is logged; it does not stop the run.
        transaction.on_commit(
            lambda proposal_uuid=proposal.uuid, state=proposal.state: (
                tasks.notify_proposal_decision(
                    proposal_uuid, ProposalStates.IN_REVIEW, state
                )
            ),
            robust=True,
        )
    return _entry(
        proposal_uuid,
        BulkStepDecisionResults.DECIDED,
        _decided_detail(proposal),
        proposal,
    )


def _record(run, position, entry):
    """Record the proposal at ``position`` of the locked run."""
    models.BulkStepDecisionResult.objects.create(run=run, position=position, **entry)
    run.processed = position + 1
    if entry["status"] == BulkStepDecisionResults.DECIDED:
        run.decided_count += 1
    else:
        run.refused_count += 1
    run.save(update_fields=["processed", "decided_count", "refused_count", "modified"])


def decision_of(run):
    user = run.created_by
    return Decision(
        call=run.call,
        call_step=run.call_step,
        outcome=run.outcome,
        outcome_reason=run.outcome_reason,
        user=user,
        staff=bool(run.acts_as_staff and user and user.is_staff and user.is_active),
    )


def _may_go_on(run):
    """Why the run may not go on deciding, or None."""
    refusal = _step_refusal(run)
    if refusal:
        return refusal
    user = run.created_by
    if user is None or not proposal_permissions.may_decide_in_bulk(
        user, run.call, run.call_step, staff=run.acts_as_staff and user.is_staff
    ):
        return USER_GONE
    return None


def fail(run, message):
    """Stop the run, unless it has stopped already, saying why."""
    models.BulkStepDecision.objects.filter(pk=run.pk, state__in=UNFINISHED).update(
        state=BulkStepDecisionStates.FAILED,
        error_message=str(message),
        completed_at=timezone.now(),
        modified=timezone.now(),
    )
    run.refresh_from_db()
    return run


def _recorded(run, position):
    return models.BulkStepDecision.objects.filter(
        pk=run.pk, processed__gt=position
    ).exists()


def _decide_and_record(locked, position):
    """Decide, or preview, the proposal at ``position`` of the locked run and
    record it in a savepoint. Whatever fails there -- the decision, or
    recording it -- is rolled back alone and the proposal is recorded as
    refused, so the run goes on with the next one."""
    decision = decision_of(locked)
    proposal_uuid = locked.proposal_uuids[position]
    try:
        with transaction.atomic():
            entry = (
                preview_one(decision, proposal_uuid)
                if locked.dry_run
                else _decide_locked(decision, proposal_uuid)
            )
            _record(locked, position, entry)
        return
    except Exception as error:
        _log_unexpected(error, decision, proposal_uuid)
        entry = _refusal_entry(proposal_uuid, error)
    # The savepoint took back what _record had counted.
    locked.refresh_from_db(fields=["processed", "decided_count", "refused_count"])
    try:
        with transaction.atomic():
            _record(locked, position, entry)
    except Exception:
        logger.exception(
            "Proposal %s of bulk step decision %s could not be recorded",
            proposal_uuid,
            locked.uuid.hex,
        )
        locked.refresh_from_db(fields=["processed", "decided_count", "refused_count"])
        _record(
            locked,
            position,
            _entry(proposal_uuid, BulkStepDecisionResults.REFUSED, UNEXPECTED),
        )


def execute(run):
    """Decide, or preview, the run's proposals still to be recorded, one by
    one. The caller must not hold a transaction.

    The run is claimed by a conditional update: a finished run is left
    alone. Each proposal is decided in a transaction holding the run's row
    lock, and is recorded in it at the first position not yet recorded: a
    second executor of the run waits for the lock, then goes on from there,
    and a run picked up again after an interruption goes on from where it
    stopped. Before each proposal, the run's step and its user's standing
    are checked again: a run whose user may no longer decide the step, or
    whose step changed, fails.
    """
    now = timezone.now()
    claimed = models.BulkStepDecision.objects.filter(
        pk=run.pk, state__in=UNFINISHED
    ).update(state=BulkStepDecisionStates.RUNNING, modified=now)
    if not claimed:
        run.refresh_from_db()
        return run
    models.BulkStepDecision.objects.filter(pk=run.pk, started_at__isnull=True).update(
        started_at=now
    )
    run.refresh_from_db()
    while True:
        position = None
        try:
            with transaction.atomic():
                locked = (
                    models.BulkStepDecision.objects.select_for_update(of=("self",))
                    .select_related("call", "call_step", "created_by")
                    .get(pk=run.pk)
                )
                if locked.state != BulkStepDecisionStates.RUNNING:
                    break
                position = locked.processed
                if position >= len(locked.proposal_uuids):
                    break
                refusal = _may_go_on(locked)
                if refusal:
                    locked.state = BulkStepDecisionStates.FAILED
                    locked.error_message = str(refusal)
                    locked.completed_at = timezone.now()
                    locked.save(
                        update_fields=[
                            "state",
                            "error_message",
                            "completed_at",
                            "modified",
                        ]
                    )
                    break
                _decide_and_record(locked, position)
        except Exception:
            # Raised once the proposal had committed -- by a callback run on
            # commit -- it is recorded, and the run goes on. Raised before,
            # nothing of it is kept, and the run stops.
            if position is None or not _recorded(run, position):
                raise
            logger.exception(
                "A callback failed after position %s of bulk step decision %s "
                "was recorded",
                position,
                run.uuid.hex,
            )
    finished = models.BulkStepDecision.objects.filter(
        pk=run.pk, state=BulkStepDecisionStates.RUNNING
    ).update(
        state=BulkStepDecisionStates.DONE,
        completed_at=timezone.now(),
        modified=timezone.now(),
    )
    run.refresh_from_db()
    if finished and not run.dry_run:
        # Only the executor that finished the run tells of it.
        _emit(run)
        logger.info(
            "Step %s decided as %s for %s proposal(s) of call %s by user %s: "
            "%s decided, %s refused",
            run.step_name,
            run.outcome,
            run.total,
            run.call.uuid.hex,
            run.created_by.uuid.hex if run.created_by else "-",
            run.decided_count,
            run.refused_count,
        )
    return run


def _emit(run):
    user = run.created_by
    event_logger.emit(
        "{actor} decided step {step_name} of call {call_name} as {outcome} "
        "for {total} proposal(s): {decided_count} decided, "
        "{refused_count} refused.",
        event_type=EventType.PROPOSAL_STEPS_DECIDED_IN_BULK,
        event_context={
            "call": run.call,
            "actor": (user.full_name or user.username) if user else "",
            "actor_uuid": user.uuid.hex if user else "",
            "step_name": run.step_name,
            "step_uuid": run.call_step.uuid.hex if run.call_step else "",
            "outcome": run.outcome,
            "outcome_reason": run.outcome_reason,
            "total": run.total,
            "decided_count": run.decided_count,
            "refused_count": run.refused_count,
            "bulk_step_decision_uuid": run.uuid.hex,
        },
        scopes=[run.call],
    )


def _dispatch(run_uuid):
    """Hand the run to the background task; a run that cannot be handed
    over fails, rather than waiting for ever."""
    try:
        tasks.run_bulk_step_decision.delay(run_uuid)
    except Exception:
        logger.exception("Bulk step decision %s could not be dispatched", run_uuid)
        run = models.BulkStepDecision.objects.filter(uuid=run_uuid).first()
        if run is not None:
            fail(run, RUN_FAILED)


def start(decision, proposal_uuids, dry_run=False):
    """Record a run deciding -- or, with ``dry_run``, previewing -- the named
    proposals, and run it: within the request for a small selection, else
    in the background. Returns the run. The caller must not hold a
    transaction."""
    uuids = unique_uuids(proposal_uuids)
    run = models.BulkStepDecision.objects.create(
        call=decision.call,
        call_step=decision.call_step,
        step_name=decision.call_step.name or decision.call_step.key,
        outcome=decision.outcome,
        outcome_reason=decision.outcome_reason,
        dry_run=dry_run,
        created_by=decision.user,
        acts_as_staff=decision.staff,
        proposal_uuids=uuids,
        total=len(uuids),
    )
    if len(uuids) > BACKGROUND_THRESHOLD:
        transaction.on_commit(lambda run_uuid=run.uuid.hex: _dispatch(run_uuid))
        # Outside a transaction it was dispatched at once, and may have
        # failed already.
        run.refresh_from_db()
        return run
    try:
        return execute(run)
    except Exception:
        logger.exception("Bulk step decision %s failed", run.uuid.hex)
        return fail(run, RUN_FAILED)


def resume_stale():
    """Look after the runs in the background.

    A run a worker had claimed that has recorded nothing for ``STALE_AFTER``
    lost its worker, or its message: it is dispatched again, and given up
    after ``MAX_ATTEMPTS``. A run no worker has claimed yet is dispatched
    again too -- a duplicate message is harmless, the run being claimed and
    locked -- without counting it: a backed-up queue is no failure of the
    run. It is given up after ``PENDING_GIVE_UP``. Previews older than
    ``PREVIEW_KEPT`` are deleted. Returns how many runs were dispatched.
    """
    now = timezone.now()
    models.BulkStepDecision.objects.filter(
        dry_run=True, created__lt=now - PREVIEW_KEPT
    ).delete()
    stale = models.BulkStepDecision.objects.filter(
        state__in=UNFINISHED, modified__lt=now - STALE_AFTER
    )
    dispatched = 0
    for run in stale:
        claimed = run.state == BulkStepDecisionStates.RUNNING
        if (claimed and run.attempts >= MAX_ATTEMPTS) or (
            not claimed and run.created < now - PENDING_GIVE_UP
        ):
            fail(run, GAVE_UP)
            continue
        updated = models.BulkStepDecision.objects.filter(
            pk=run.pk, state=run.state, modified=run.modified
        ).update(
            attempts=F("attempts") + (1 if claimed else 0), modified=timezone.now()
        )
        if updated:
            _dispatch(run.uuid.hex)
            dispatched += 1
    return dispatched


def runs_visible_to(user, queryset=None):
    """The runs ``user`` reads: every one for staff and support; those of
    the calls whose proposals they drive through the workflow
    (``permissions.manages_call_workflow``): as call manager, or holding
    CALL.APPROVE_AND_REJECT_PROPOSALS on the call's managing organisation;
    and the ones they started, while they still hold a role on the call."""
    queryset = models.BulkStepDecision.objects.all() if queryset is None else queryset
    if user.is_staff or user.is_support:
        return queryset
    organisations = get_scope_ids(
        user,
        ContentType.objects.get_for_model(models.CallManagingOrganisation),
        permission=PermissionEnum.APPROVE_AND_REJECT_PROPOSALS,
    )
    return queryset.filter(
        Q(created_by=user, call__in=get_connected_calls(user))
        | Q(call__in=get_connected_calls(user, CallRole.MANAGER))
        | Q(call__manager_id__in=organisations)
    )


def hidden_from(user, run):
    """The uuids of the run's proposals ``user`` applies with, whose results
    are not shown to them, and whether any of those has its decision held:
    then the run's reason is not shown to them either."""
    if user is None or not user.is_authenticated or user.is_staff or user.is_support:
        return set(), False
    proposals = list(
        models.Proposal.objects.filter(
            pk__in=proposal_roles.applicant_team_proposal_ids(user),
            uuid__in=run.proposal_uuids,
        ).values_list("uuid", "decision_held")
    )
    return (
        {proposal_uuid.hex for proposal_uuid, _ in proposals},
        any(held for _, held in proposals),
    )
