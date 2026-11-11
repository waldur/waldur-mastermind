"""Workflow step transition helpers.

Centralises the logic that advances a proposal's workflow when a step is
completed, rejected, or expires. Callers must hold a row-level lock on the
active step instance (via select_for_update) before invoking these helpers.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Avg, Q
from django.utils import timezone

from waldur_core.core.utils import get_system_robot
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_mastermind.proposal import (
    activity,
    budgets,
    change_requests,
    contributions,
    dry_run_context,
    models,
    notification_rules,
    panels,
    proposal_roles,
    round_reports,
    runs,
    utils,
)
from waldur_mastermind.proposal import (
    permissions as proposal_permissions,
)
from waldur_mastermind.proposal.enums import (
    ChangeRequestResolutions,
    ContributorSources,
    NotificationRuleTriggers,
    ProposalRunStates,
    ProposalStates,
    StepContributionStatuses,
    StepKinds,
    TransitionModes,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)

logger = logging.getLogger(__name__)


def withholds_feedback(call_step, outcome_reason) -> bool:
    """Whether the outcome reason given on completing ``call_step`` waits for
    the step's report to be released before the applicant reads it: only a
    step whose report is released at all, so feedback is never withheld for
    good."""
    return bool(
        outcome_reason
        and call_step.kind == StepKinds.ASSESSMENT
        and call_step.release_to_applicant
    )


def _announce(instance, trigger):
    """Tell of a step's start or end: its notification rules' mail, and its
    proposal's activity."""
    notification_rules.dispatch_step_event(instance, trigger)
    activity.log_step_event(instance, trigger)


def _step_label(instance):
    """Human-readable step name for error messages: the call step's name."""
    return instance.call_step.name or instance.step


def _ordered_instances(proposal, run=None):
    """The proposal's step instances in the order of its own path.

    Each instance keeps the order its call step had when the proposal was
    submitted, so the path does not change when the call's steps are
    reordered later. The path is the evaluation's, or with ``run`` (a
    ``ProposalRun`` or its id) that post-award run's: the two never mix.
    """
    instances = proposal.workflow_step_instances.select_related("call_step")
    if run is None:
        instances = instances.filter(run__isnull=True)
    else:
        instances = instances.filter(run=run)
    return instances.order_by("order", "id")


def allocation_decision_instance(proposal):
    """The proposal's instance of its allocation decision, if it has one.

    Read from the instances, which record which step settled the award when
    the proposal was submitted. A call has at most one enabled allocation
    decision; a disabled one leaves a skipped instance, which is never the
    decision the proposal waits on.
    """
    return (
        _ordered_instances(proposal)
        .filter(is_allocation_decision=True)
        .exclude(status=WorkflowStepInstanceStatuses.SKIPPED)
        .first()
    )


def _merge_notes(existing, addition):
    """Append a new note to existing internal notes with a blank-line break.

    Notes accumulate across the step's lifecycle (e.g. a manager records a
    holding note before deciding on the outcome). Overwriting on every save
    would silently discard earlier context.
    """
    if not existing:
        return addition
    return f"{existing}\n\n{addition}"


def active_instances(proposal):
    """The proposal's active step instances, in the order of its path.

    Several when the proposal stands in a stage of steps that run at the same
    time; ``proposal.current_step`` is the first of them. The evaluation is
    over before a post-award run opens, so they are of one path, whichever
    it is.
    """
    return (
        proposal.workflow_step_instances.select_related("call_step")
        .filter(status=WorkflowStepInstanceStatuses.ACTIVE)
        .order_by("order", "id")
    )


def standing_label(proposal):
    """Where the proposal stands, for people: the names of every step of its
    stage still running, else of the step it is parked on; None outside the
    workflow."""
    names = [i.call_step.name or i.step for i in active_instances(proposal)]
    if names:
        return " · ".join(names)
    current = proposal.current_step
    if current is None:
        return None
    return current.call_step.name or current.step


def _stage_members(proposal, instance):
    """The proposal's instances in ``instance``'s stage, ``instance`` too."""
    instances = _ordered_instances(proposal, instance.run_id)
    if instance.stage is None:
        # Submitted before stages: every step is a stage of its own.
        return instances.filter(pk=instance.pk)
    return instances.filter(stage=instance.stage)


def _skip_unmet(instance):
    """Skip a step whose run condition does not hold on the proposal.

    It never starts: no notification goes out and no contribution is opened.
    """
    instance.status = WorkflowStepInstanceStatuses.SKIPPED
    instance.completed_at = timezone.now()
    instance.save(update_fields=["status", "completed_at"])
    activity.log_step_skipped(instance)


def _next_stage(proposal, after=None, run=None):
    """The instances of the next stage the proposal should enter.

    Source of truth is the ``ProposalWorkflowStepInstance`` set fixed at
    submit, each with the order, stage and run condition its call step had
    then. Later edits to the call's ``CallWorkflowStep`` config -- enabling a
    new step, disabling a downstream step, reordering the steps, moving the
    allocation decision, changing a condition -- do not alter a proposal's
    path; they apply to proposals submitted afterwards.

    Walks the PENDING instances after ``after`` (from the start without it)
    in that order. One whose run condition does not hold now is skipped, and
    the walk goes on; the first one that runs gives the stage, and the
    stage's other pending instances that run join it. Empty when no step is
    left to run. The walk stays on one path: ``after``'s, else the
    evaluation, or the post-award ``run``.
    """
    if after is not None:
        run = after.run_id
    candidates = _ordered_instances(proposal, run).filter(
        status=WorkflowStepInstanceStatuses.PENDING
    )
    if after is not None:
        candidates = candidates.filter(
            Q(order__gt=after.order) | Q(order=after.order, id__gt=after.pk)
        )
    members, stage = [], None
    for candidate in candidates:
        if stage is not None and candidate.stage_key != stage:
            break
        if not contributions.run_condition_holds(candidate):
            _skip_unmet(candidate)
            continue
        stage = candidate.stage_key
        members.append(candidate)
    return members


def next_stage_preview(proposal, after=None):
    """The instances of the stage the proposal would enter next, as
    ``_next_stage`` walks them, without skipping anything: for telling where
    a proposal would go before moving it."""
    candidates = _ordered_instances(proposal).filter(
        status=WorkflowStepInstanceStatuses.PENDING
    )
    if after is not None:
        candidates = candidates.filter(
            Q(order__gt=after.order) | Q(order=after.order, id__gt=after.pk)
        )
    members, stage = [], None
    for candidate in candidates:
        if stage is not None and candidate.stage_key != stage:
            break
        if not contributions.run_condition_holds(candidate):
            continue
        stage = candidate.stage_key
        members.append(candidate)
    return members


def _enter_stage(proposal, members):
    """Start every step of a stage at once; the proposal stands in its first.

    The one place a proposal enters steps: its first stage, the stage after
    one that ended, and the stage after a lapse all come through here, and
    steps whose run condition does not hold were skipped before it
    (``_next_stage``).
    """
    proposal.current_step = members[0]
    proposal.save(update_fields=["current_step"])
    for member in members:
        _activate_next_step(proposal, member)
    # Every member runs before an automatic decision among them is taken, so
    # the decision's completion sees the rest of its stage still active.
    for member in members:
        if _enter_step(proposal, member) is None:
            return _first_still_active(proposal)
    return proposal.current_step


def _enter_step(proposal, instance):
    """Take an automatic allocation decision on a step the proposal has just
    entered.

    Returns the step instance when it stays active, or None when the
    automatic decision completed it.
    """
    if not (
        instance.is_allocation_decision and instance.call_step.decides_automatically
    ):
        return instance
    if _decide_automatically(proposal, instance) is instance:
        return instance
    return None


def _first_still_active(proposal):
    """The step the proposal goes on in after an automatic decision, or None
    when the decision ended the workflow or is held for the round."""
    proposal.refresh_from_db(fields=["state", "current_step", "decision_held"])
    if proposal.state != ProposalStates.IN_REVIEW or proposal.decision_held:
        return None
    return active_instances(proposal).first()


def _stage_still_running(proposal, instance):
    """Whether a step of ``instance``'s stage is still active, ``instance``
    having just ended. If so the proposal now stands in the first of them."""
    sibling = active_instances(proposal).exclude(pk=instance.pk).first()
    if sibling is None:
        return False
    if proposal.current_step_id != sibling.pk:
        proposal.current_step = sibling
        proposal.save(update_fields=["current_step"])
    return True


def _parking_member(proposal, instance):
    """The completed step of ``instance``'s stage that waits for the call
    managers to move the proposal on (manual transition), or None."""
    for member in _stage_members(proposal, instance):
        if (
            member.status == WorkflowStepInstanceStatuses.COMPLETED
            and member.call_step.transition_mode == TransitionModes.MANUAL
        ):
            return member
    return None


def _withdraw_siblings(proposal, instance):
    """Stop the steps still running beside ``instance``, whose outcome ended
    the workflow: their open work, and the changes they asked of the
    applicant, are withdrawn and nobody is told, as the proposal's end is
    announced on its own."""
    siblings = (
        active_instances(proposal)
        .exclude(pk=instance.pk)
        .select_for_update(of=("self",))
    )
    now = timezone.now()
    for sibling in siblings:
        sibling.status = WorkflowStepInstanceStatuses.WITHDRAWN
        sibling.completed_at = now
        sibling.save(update_fields=["status", "completed_at"])
        contributions.withdraw_open(sibling)
        # Nor are the changes asked of the applicant there.
        change_requests.close_open_of_step(sibling, ChangeRequestResolutions.CANCELLED)
        activity.log_step_withdrawn(sibling)


def _activate_next_step(proposal, instance):
    """Activate a pending step instance, set its deadline, return it.

    Duration is read live from the ``CallWorkflowStep`` so admins can adjust
    deadlines before activation.
    """
    call_step = instance.call_step
    instance.status = WorkflowStepInstanceStatuses.ACTIVE
    instance.started_at = timezone.now()
    round_step = round_reports.round_step_of(proposal.round, call_step)
    instance.deadline = step_deadline(instance, round_step)
    instance.save(update_fields=["status", "started_at", "deadline"])
    if call_step.round_scoped:
        # Run once for the round: no work of its own on the proposal, and
        # nobody is told per proposal. Its people hear when the call's
        # managers open it; one arriving after that joins its reports.
        activity.log_step_event(instance, NotificationRuleTriggers.STEP_STARTED)
        if round_step is not None and round_step.is_open:
            round_step = models.RoundStep.objects.select_for_update().get(
                pk=round_step.pk
            )
            created = round_reports.sync_reports(round_step)
            if created:
                round_reports.announce_reports(round_step, created)
        return instance
    # The contributions known up front; the rest are made as people arrive.
    contributions.create_contributions(instance)
    # The chair of the proposal's panel takes the roles held by its chair.
    assign_panel_chairs(instance)
    # The allocation decision edits an award, which starts as the request.
    if instance.is_allocation_decision:
        utils.prefill_awarded_resources(proposal)
    # So does an extension's decision: what it grants starts as what was asked.
    if instance.run_id and call_step.kind == StepKinds.DECISION:
        runs.prefill_granted(instance.run)
    _announce(instance, NotificationRuleTriggers.STEP_STARTED)
    return instance


def assign_panel_chairs(instance):
    """Assign the proposal's panel chair on an assignment step that starts,
    told as an assignment made by hand is, and logged as no one's: the
    workflow made it."""
    for assignment in proposal_roles.assign_panel_chairs(instance):
        with activity.without_actor():
            activity.log_evaluator_assigned(assignment)
        notification_rules.dispatch_role_assigned(assignment)


def step_deadline(instance, round_step=None):
    """When the step ``instance`` is due: its round's due date for the step
    when one is set, else its duration from when it started -- for a round
    step, from when it was opened for the round; None before that."""
    if round_step is not None and round_step.due_at is not None:
        return round_step.due_at
    duration = instance.call_step.duration_in_days
    if not duration:
        return None
    if instance.call_step.round_scoped:
        if round_step is None or round_step.opened_at is None:
            return None
        return round_step.opened_at + timedelta(days=duration)
    if instance.started_at is None:
        return None
    return instance.started_at + timedelta(days=duration)


def _decision_is_withheld(proposal, instance):
    """Whether the outcome of ``instance`` must be held until the round publishes.

    Locks the round row, so a decision and the round's publication serialise:
    a decision either lands before publication and is released by it, or
    after and is announced at once. Publication locks the round's proposals
    before the round, and every caller here holds the proposal's row lock
    before it gets here (the step actions in views, the expiry sweep in
    tasks), so the order is proposal, then round everywhere and the two
    cannot deadlock.
    """
    if not instance.is_allocation_decision:
        return False
    call_round = (
        models.Round.objects.select_for_update()
        .select_related("call")
        .get(pk=proposal.round.pk)
    )
    return call_round.withholds_results


def _hold_decision(proposal):
    """Keep a decided proposal ``in_review`` until its round publishes."""
    proposal.decision_held = True
    proposal.save(update_fields=["decision_held"])
    activity.log_decision_held(proposal)


def create_step_instances(proposal):
    """Fix the proposal's workflow path at submission.

    One instance per call step: ``pending`` where the step is enabled,
    ``skipped`` otherwise, each with a copy of the step's order, its stage,
    its run condition, whether it is the allocation decision and which steps
    it reads. Later edits to the call's step configuration do not alter this
    path (see ``_next_stage``). Only the evaluation's steps: those of an
    extension or closing are instantiated for each run (``open_run``).
    """
    _instantiate_steps(proposal, WorkflowPhases.EVALUATION)


def _instantiate_steps(proposal, phase, run=None):
    """Create the proposal's instances of the call's ``phase`` steps, for
    ``run`` when the phase is a post-award one."""
    call_steps = list(
        models.CallWorkflowStep.objects.filter(call=proposal.round.call, phase=phase)
        .prefetch_related("input_steps")
        .order_by("order", "id")
    )
    stages = models.CallWorkflowStep.stages(call_steps)
    instances = models.ProposalWorkflowStepInstance.objects.bulk_create(
        [
            models.ProposalWorkflowStepInstance(
                proposal=proposal,
                run=run,
                call_step=call_step,
                step=call_step.key,
                order=call_step.order,
                stage=stages[call_step.pk],
                run_condition=dict(call_step.run_condition or {}),
                is_allocation_decision=call_step.is_allocation_decision,
                status=(
                    WorkflowStepInstanceStatuses.PENDING
                    if call_step.is_enabled
                    else WorkflowStepInstanceStatuses.SKIPPED
                ),
            )
            for call_step in call_steps
        ]
    )
    # Which earlier steps each step reads is fixed with the path, too.
    through = models.ProposalWorkflowStepInstance.input_steps.through
    through.objects.bulk_create(
        [
            through(
                proposalworkflowstepinstance_id=instance.pk,
                callworkflowstep_id=input_step.pk,
            )
            for instance, call_step in zip(instances, call_steps)
            for input_step in call_step.input_steps.all()
        ]
    )
    return instances


def activate_first_step(proposal):
    """Start a submitted proposal's evaluation: activate its first stage.

    Shared by submission (when the call evaluates on submission) and by the
    cut-off task (when it evaluates at the round's cut-off). The deadlines run
    from now, and the step-started notification rules fire through their
    ledger. Moves the proposal to ``in_review``. Returns the first active
    instance, or None when the proposal has no step to run, in which case it
    is left as it is. The caller holds the proposal's row lock inside a
    transaction.
    """
    # The proposal goes to the panel covering its domain first: the first
    # stage may already staff it from that panel.
    if panels.route(proposal) is not None:
        activity.log_panel_changed(proposal, None, None)
    members = _next_stage(proposal)
    if not members:
        return None
    proposal.state = ProposalStates.IN_REVIEW
    proposal.save(update_fields=["state"])
    return _enter_stage(proposal, members)


def in_workflow(proposal) -> bool:
    """Whether the proposal's steps can be acted on: in review, or accepted
    with a post-award run open."""
    if proposal.state == ProposalStates.IN_REVIEW:
        return True
    return (
        proposal.state == ProposalStates.ACCEPTED
        and runs.open_run_of(proposal) is not None
    )


class RunError(runs.RunError):
    """A post-award run that cannot be opened or withdrawn."""


@transaction.atomic
def open_run(
    proposal, phase, user=None, requested_end_date=None, requested_limits=None
):
    """Open an extension request or the final report on an accepted proposal.

    Locks the proposal and checks again under the lock that a run of
    ``phase`` may be opened now (``runs.open_refusal``). Creates the run and
    an instance of each of the phase's call steps for it, pending or
    skipped as at submission, and starts its first stage. The proposal
    stays accepted. ``user`` None is the system opening the final report
    when it is due. Returns the run.
    """
    proposal = (
        models.Proposal.objects.select_for_update(of=("self",))
        .select_related("project", "round__call")
        .get(pk=proposal.pk)
    )
    refusal = runs.open_refusal(proposal, phase)
    if refusal:
        raise RunError(refusal)
    run = models.ProposalRun.objects.create(
        proposal=proposal,
        phase=phase,
        opened_by=user,
        requested_end_date=requested_end_date,
        requested_limits=requested_limits or {},
    )
    _instantiate_steps(proposal, phase, run)
    members = _next_stage(proposal, run=run)
    if not members:
        raise RunError(f"No {phase} step runs on this proposal.")
    activity.log_run_opened(run)
    _enter_stage(proposal, members)
    return run


@transaction.atomic
def withdraw_run(proposal, run, user):
    """Call off an open run: its running steps are withdrawn, their open
    work with them, and nothing is granted. The caller has checked who may.
    Locks the proposal, then the run."""
    models.Proposal.objects.select_for_update(of=("self",)).get(pk=proposal.pk)
    run = models.ProposalRun.objects.select_for_update().get(pk=run.pk)
    if not run.is_open:
        raise RunError("The run is already closed.")
    proposal.refresh_from_db()
    now = timezone.now()
    for instance in run.step_instances.filter(
        status=WorkflowStepInstanceStatuses.ACTIVE
    ).select_for_update(of=("self",)):
        instance.status = WorkflowStepInstanceStatuses.WITHDRAWN
        instance.completed_at = now
        instance.save(update_fields=["status", "completed_at"])
        contributions.withdraw_open(instance)
    runs.close_run(proposal, run, ProposalRunStates.WITHDRAWN, user)
    return run


@transaction.atomic
def complete_step(
    proposal,
    current_instance,
    outcome,
    outcome_reason,
    completed_by,
    internal_notes="",
    budget_override_reason="",
):
    """Complete an active step. Advance only if transition_mode is automatic.

    Returns the newly active next step instance, or None if the workflow
    terminated (proposal accepted), if the step is awaiting manual advance,
    OR if other steps of its stage are still running. Callers should inspect
    the proposal's state and ``is_awaiting_manual_advance(proposal)`` to
    distinguish those cases.

    A step that runs beside others in its stage completes on its own; the
    stage moves on once the last of them ends, and parks for the call
    managers when any of them advances manually.

    ``internal_notes`` is a call-management-team-only free-text field; it is
    appended (not overwritten) so completing a step never erases notes
    captured earlier in the same step's lifecycle.

    Raises ``ValueError`` if the step's gates (its form, ``min_contributions``
    / ``min_score_threshold``) are not yet satisfied,
    or if the outcome is not the one the step's contributions add up to.
    An allocation decision approving an award that does not fit what remains
    of the call's budgets raises ``budgets.BudgetExceeded`` unless
    ``budget_override_reason`` overrides it (``budgets.enforce``).

    A negative outcome rejects the proposal, or cancels it, as the step's
    ``on_negative`` says -- unless it says ``continue``: then the step is
    completed with its negative outcome and the proposal goes on to the next.
    """
    call_step = current_instance.call_step
    _enforce_no_open_change_request(current_instance)
    _enforce_run_request(current_instance)
    _enforce_derived_outcome(current_instance, outcome, call_step)
    _enforce_step_gates(proposal, current_instance, outcome, call_step)
    _enforce_award_amounts(proposal, current_instance, outcome)
    if (
        current_instance.is_allocation_decision
        and outcome not in WorkflowStepOutcomes.NEGATIVE_OUTCOMES
    ):
        budgets.enforce(proposal, completed_by, budget_override_reason)

    current_instance.status = WorkflowStepInstanceStatuses.COMPLETED
    current_instance.outcome = outcome
    current_instance.outcome_reason = outcome_reason or ""
    # Feedback on a step whose report is released to the applicant waits for
    # that release, whether the step was completed on the proposal or for the
    # whole round.
    current_instance.feedback_withheld = withholds_feedback(
        call_step, current_instance.outcome_reason
    )
    current_instance.completed_at = timezone.now()
    current_instance.completed_by = completed_by
    if internal_notes:
        current_instance.internal_notes = _merge_notes(
            current_instance.internal_notes, internal_notes
        )
    current_instance.save(
        update_fields=[
            "status",
            "outcome",
            "outcome_reason",
            "feedback_withheld",
            "internal_notes",
            "completed_at",
            "completed_by",
        ]
    )
    # The form the completer filled is the step's output; work still open
    # when the step ends is no longer wanted.
    contributions.submit_completer_form(current_instance, completed_by)
    contributions.withdraw_open(current_instance)
    if _decision_is_withheld(proposal, current_instance):
        # Recorded on the instance; the step's notifications, the proposal's
        # final state and anything that follows wait for publication.
        _hold_decision(proposal)
        return None

    # A negative outcome that ends the workflow is a rejection for
    # notification purposes even though it arrives through complete_step.
    ends_workflow = (
        outcome in WorkflowStepOutcomes.NEGATIVE_OUTCOMES
        and not call_step.continues_on_negative
    )
    _announce(
        current_instance,
        NotificationRuleTriggers.STEP_REJECTED
        if ends_workflow
        else NotificationRuleTriggers.STEP_COMPLETED,
    )

    # A negative outcome (declined / ineligible / infeasible) terminates the
    # workflow instead of advancing or allocating — regardless of transition
    # mode or whether downstream steps remain. The step decides whether that
    # cancels the proposal (the applicant declining the award) or rejects it.
    # The instance keeps its true outcome (unlike reject_at_step, which
    # overwrites it with REJECTED).
    if ends_workflow:
        # The steps running beside it are no longer wanted.
        _withdraw_siblings(proposal, current_instance)
        if current_instance.run_id:
            # A post-award run ends; the accepted proposal stays accepted.
            runs.close_run(
                proposal,
                current_instance.run,
                ProposalRunStates.REJECTED,
                completed_by,
            )
            return None
        if call_step.cancels_on_negative:
            proposal.state = ProposalStates.CANCELED
        else:
            proposal.state = ProposalStates.REJECTED
        proposal.current_step = None
        proposal.save(update_fields=["state", "current_step"])
        return None

    if _stage_still_running(proposal, current_instance):
        return None

    parked = _parking_member(proposal, current_instance)
    if parked is not None:
        # The proposal stands on the step that waits, so the manual advance
        # endpoint and UI can locate it.
        if proposal.current_step_id != parked.pk:
            proposal.current_step = parked
            proposal.save(update_fields=["current_step"])
        return None

    return _advance_to_next(proposal, current_instance, acting_user=completed_by)


def check_approval_gates(proposal, instance):
    """Raise ``ValueError`` when approving the active step ``instance`` would
    be refused by its gates, the budgets aside. Writes nothing: an award not
    yet prefilled is checked when the step is completed."""
    outcome = WorkflowStepOutcomes.APPROVED
    _enforce_no_open_change_request(instance)
    _enforce_derived_outcome(instance, outcome, instance.call_step)
    _enforce_step_gates(proposal, instance, outcome, instance.call_step)
    if proposal.awarded_resources_prefilled_at is not None:
        missing = proposal.offerings_missing_awarded_amounts()
        if missing:
            raise ValueError(
                f"Cannot complete {_step_label(instance)}: awarded amounts are "
                f"missing for the following offerings: {', '.join(missing)}."
            )
        missing = proposal.offerings_missing_awarded_options()
        if missing:
            raise ValueError(
                f"Cannot complete {_step_label(instance)}: required options are "
                f"missing for the following offerings: {', '.join(missing)}."
            )


def _enforce_no_open_change_request(current_instance):
    """Refuse to decide a step while it waits on the applicant's changes:
    the request is cancelled first, so a step has one meaning at a time."""
    if open_change_request(current_instance) is not None:
        raise ValueError(
            f"Changes have been requested at {_step_label(current_instance)}: "
            "wait for the applicant to submit them, or cancel the request first."
        )


def _enforce_run_request(current_instance):
    """An extension request is submitted once it says what it asks for: a
    new end date, or new limits."""
    run = current_instance.run
    if (
        run is None
        or run.phase != WorkflowPhases.EXTENSION
        or current_instance.call_step.kind != StepKinds.APPLICANT_ACTION
    ):
        return
    if run.requested_end_date is None and not run.requested_limits:
        raise ValueError(
            f"Cannot submit {_step_label(current_instance)}: name the end date "
            "or the limits the extension asks for first."
        )


def _enforce_derived_outcome(current_instance, outcome, call_step):
    """Refuse an outcome other than the one the contributions add up to.

    Only for an outcome set whose step outcome is derived, and only once a
    contribution has been submitted; before that the outcome is the
    completer's to pick.
    """
    if not call_step.derives_outcome:
        return
    derived = contributions.derive_step_outcome(current_instance)
    if derived is not None and outcome != derived:
        raise ValueError(
            f"Cannot complete {_step_label(current_instance)} as {outcome}: "
            f"its contributions add up to {derived}."
        )


def _enforce_step_gates(proposal, current_instance, outcome, call_step):
    """Block completion of a gated step until its thresholds are met.

    ``CallWorkflowStep.min_contributions`` / ``min_score_threshold`` are the
    single enforced source of truth for
    "enough work" and "high enough average score". On a step with
    contributors they count this step's *submitted* contributions (and the
    reviews linked to them); on a step without, the proposal's submitted
    reviews, as before steps had contributions. A ``DECLINED`` outcome is
    never blocked — declining a weak proposal must always be possible
    regardless of how much work came in.
    """
    if outcome == WorkflowStepOutcomes.DECLINED:
        return

    # Holders gate: an assignment step is done once its role has the holders
    # it needs.
    missing = proposal_roles.missing_holders(proposal, call_step)
    if missing:
        raise ValueError(f"Cannot complete {_step_label(current_instance)}: {missing}")

    # Lead gate: a step only the lead contributes to waits for one.
    missing = proposal_roles.missing_lead(proposal, call_step)
    if missing:
        raise ValueError(f"Cannot complete {_step_label(current_instance)}: {missing}")

    # Checklist gate: a step with an attached, required checklist cannot be
    # completed until every required question is answered in the step's
    # contributions. Advisory checklists (checklist_required=False) never
    # block. A round step's form is answered in its round reports, which
    # are submitted only once it is.
    if (
        call_step.checklist_id
        and call_step.checklist_required
        and not call_step.round_scoped
    ):
        unanswered = contributions.unanswered_required_questions(current_instance)
        if unanswered.exists():
            raise ValueError(
                f"Cannot complete {_step_label(current_instance)}: "
                f"{unanswered.count()} required checklist "
                f"{'question is' if unanswered.count() == 1 else 'questions are'} "
                "unanswered."
            )

    # Every requested offering's contribution is in, or out of the work.
    if call_step.per_requested_offering:
        open_slots = current_instance.contributions.filter(
            requested_offering__isnull=False,
            status__in=StepContributionStatuses.OPEN,
        ).count()
        if open_slots:
            raise ValueError(
                f"Cannot complete {_step_label(current_instance)}: "
                f"{open_slots} requested "
                f"{'offering is' if open_slots == 1 else 'offerings are'} "
                "still to be assessed."
            )

    # ``max_contributions`` is no gate: it is enforced where a contribution
    # is submitted (``contributions.submit``), so a step never ends up with
    # more than it can complete with. Reviews are submitted on the review
    # itself, outside the step's control; refusing to complete a step over
    # its maximum would leave it stuck for good.
    if call_step.min_contributions is None and call_step.min_score_threshold is None:
        return

    submitted, count, noun = _gate_work(current_instance)
    if call_step.min_contributions is not None and count < call_step.min_contributions:
        raise ValueError(
            f"Cannot complete {_step_label(current_instance)}: "
            f"{count} of {call_step.min_contributions} required {noun} "
            "have been submitted."
        )
    if call_step.min_score_threshold is not None:
        avg_score, source = gate_score(current_instance)
        if avg_score is None or avg_score < call_step.min_score_threshold:
            if source is not None:
                raise ValueError(
                    f"Cannot complete {_step_label(current_instance)}: the "
                    f"score from {_step_label(source)} ({_format_score(avg_score)}) "
                    "is below the required minimum of "
                    f"{call_step.min_score_threshold}."
                )
            raise ValueError(
                f"Cannot complete {_step_label(current_instance)}: the average review "
                f"score ({avg_score if avg_score is not None else 0}) is below "
                f"the required minimum of {call_step.min_score_threshold}."
            )


def _gate_work(instance):
    """The submitted reviews a step's gates read, how much work came in, and
    what to call it.

    On a step with contributors, this step's own work; on a step without,
    the proposal's submitted reviews, as before steps had contributions.
    """
    call_step = instance.call_step
    if call_step.contributor_source_kind == ContributorSources.ASSIGNED_REVIEWERS:
        # A reviewer's contribution is their review.
        submitted = models.Review.objects.filter(
            contribution__proposal_step=instance,
            contribution__status=StepContributionStatuses.SUBMITTED,
            state=models.Review.States.SUBMITTED,
        )
        return submitted, submitted.count(), "reviews"
    if call_step.collects_contributions:
        submitted_contributions = contributions.counted_contributions(instance)
        submitted = models.Review.objects.filter(
            contribution__in=submitted_contributions,
            state=models.Review.States.SUBMITTED,
        )
        return submitted, submitted_contributions.count(), "contributions"
    submitted = instance.proposal.review_set.filter(
        state=models.Review.States.SUBMITTED
    )
    return submitted, submitted.count(), "reviews"


def _has_own_work(call_step) -> bool:
    return (
        call_step.contributor_source_kind == ContributorSources.ASSIGNED_REVIEWERS
        or call_step.collects_contributions
    )


def _format_score(score) -> str:
    return "none yet" if score is None else f"{float(score):.2f}"


def gate_score(instance):
    """The score ``instance``'s ``min_score_threshold`` is checked against,
    and the input step instance it comes from (None: not an input step).

    A step without contributors that reads input steps takes the score of
    the latest completed input step that has one (``contributions.step_score``):
    a consensus grading made after the experts' reviews outranks them, and
    the reviews count when no grading was made. Otherwise -- no input step
    scored the proposal, the step has none, or it has work of its own -- the
    average of the reviews ``_gate_work`` reads, as before.
    """
    if not _has_own_work(instance.call_step):
        for source in reversed(list(contributions.input_instances(instance))):
            if source.status != WorkflowStepInstanceStatuses.COMPLETED:
                continue
            score = contributions.step_score(source)
            if score is not None:
                return score, source
    submitted, _, _ = _gate_work(instance)
    return submitted.aggregate(avg=Avg("summary_score"))["avg"], None


def score_gate(instance):
    """The active step's score against its ``min_score_threshold``, for the
    call team to see before deciding; None without a threshold."""
    threshold = instance.call_step.min_score_threshold
    if threshold is None:
        return None
    score, source = gate_score(instance)
    return {
        "score": None if score is None else round(float(score), 2),
        "threshold": threshold,
        "source_step_name": _step_label(source) if source is not None else None,
        "passes": score is not None and score >= threshold,
    }


def is_ready_to_complete(user, instance) -> bool:
    """Whether the active step ``instance`` waits only for ``user`` to
    complete it: they may act on it, none of its contributions is still
    open, at least one was submitted, and its gates are met.

    A step is never completed for its completer, as completing may carry
    an outcome decision: this only tells them their turn has come.
    """
    proposal = instance.proposal
    if instance.status != WorkflowStepInstanceStatuses.ACTIVE:
        return False
    if not proposal_permissions.user_can_act_on_step(user, instance):
        return False
    if open_change_request(instance) is not None:
        return False
    statuses = set(instance.contributions.values_list("status", flat=True))
    if statuses & set(StepContributionStatuses.OPEN):
        return False
    if StepContributionStatuses.SUBMITTED not in statuses:
        return False
    try:
        _enforce_step_gates(proposal, instance, "", instance.call_step)
    except ValueError:
        return False
    return True


def complete_from_contributions(proposal, instance, completed_by):
    """Complete a step whose outcome its contributions decide, once all are in.

    For a step with a derived outcome set: when no contribution is open and
    at least one was submitted, the step is completed with the outcome they
    add up to, by whoever submitted the last one. A gate that still holds
    (too few contributions, say) leaves the step active for the completer.
    Returns ``(completed, next_instance)``. The caller holds the proposal's
    and the step's row locks inside a transaction.
    """
    call_step = instance.call_step
    if (
        not call_step.derives_outcome
        or call_step.round_scoped
        or instance.status != WorkflowStepInstanceStatuses.ACTIVE
        or instance.proposal_id != proposal.pk
        or instance.contributions.filter(
            status__in=StepContributionStatuses.OPEN
        ).exists()
    ):
        return False, None
    outcome = contributions.derive_step_outcome(instance)
    if outcome is None:
        return False, None
    try:
        with transaction.atomic():
            next_instance = complete_step(
                proposal=proposal,
                current_instance=instance,
                outcome=outcome,
                outcome_reason="",
                completed_by=completed_by,
            )
    except ValueError as exc:
        logger.info(
            "Step %s of proposal %s stays active after its last contribution: %s",
            instance.uuid.hex,
            proposal.uuid.hex,
            exc,
        )
        return False, None
    return True, next_instance


def settle_evaluator_slots(proposal, user, acted_by=None):
    """Bring ``user``'s slots on the proposal's evaluator-role steps in line
    with what they may do now (``contributions.sync_role_slots``), and
    complete an active step when taking theirs out of its work left its
    outcome to the contributions already in.

    Takes the proposal's and the active steps' row locks before touching a
    slot -- the order of the step actions: proposal, step, contribution --
    inside the caller's transaction when there is one, so a contribution
    being submitted, or a step being completed, meanwhile is waited for.
    Returns ``(completed, next_instance)``.
    """
    with transaction.atomic():
        proposal = models.Proposal.objects.select_for_update(of=("self",)).get(
            pk=proposal.pk
        )
        instance_ids = list(
            active_instances(proposal)
            .select_for_update(of=("self",))
            .values_list("pk", flat=True)
        )
        if not contributions.sync_role_slots(user, proposal) or not instance_ids:
            return False, None
        completed, next_instance = False, None
        for instance_id in instance_ids:
            # Re-read: completing one step of a stage may have ended another.
            instance = models.ProposalWorkflowStepInstance.objects.select_related(
                "call_step"
            ).get(pk=instance_id)
            done, after = complete_from_contributions(proposal, instance, acted_by)
            if done:
                completed, next_instance = True, after
        return completed, next_instance


def _enforce_award_amounts(proposal, current_instance, outcome):
    """Block approving an award that grants an offering no amount, or leaves
    one of its required options empty.

    The award editor refuses such an item, but one can still be there: copied
    from a request written before amounts were required, or seeded around the
    API. Approving it would provision a resource with no quota. The rule is
    the one a proposal is submitted under. Declining is never blocked.
    """
    if (
        not current_instance.is_allocation_decision
        or outcome in WorkflowStepOutcomes.NEGATIVE_OUTCOMES
    ):
        return
    # A proposal that reached the step without its activation has no award
    # yet; allocation would copy one from the request, so check that copy.
    utils.prefill_awarded_resources(proposal)
    missing = proposal.offerings_missing_awarded_amounts()
    if missing:
        raise ValueError(
            f"Cannot complete {_step_label(current_instance)}: awarded "
            f"amounts are missing for the following offerings: "
            f"{', '.join(missing)}."
        )
    missing = proposal.offerings_missing_awarded_options()
    if missing:
        raise ValueError(
            f"Cannot complete {_step_label(current_instance)}: required "
            f"options are missing for the following offerings: "
            f"{', '.join(missing)}."
        )


def _award_approver(proposal, acting_user):
    """Who approved the award: the completer of the proposal's allocation
    decision -- the system for an automatic one.

    Never whoever took the proposal past a step after it, such as the
    applicant accepting the award or a provider confirming its part: the
    approver is recorded on the proposal and becomes the consumer reviewer of
    its orders, on behalf of the call's organisation. A workflow without a
    decided allocation decision records the user who ended it, unless they
    are on the applicant team, and otherwise the system.
    """
    decision = allocation_decision_instance(proposal)
    if (
        decision is not None
        and decision.status == WorkflowStepInstanceStatuses.COMPLETED
        and decision.completed_by_id
    ):
        return decision.completed_by
    if acting_user is not None and not proposal_roles.is_on_applicant_team(
        acting_user, proposal
    ):
        return acting_user
    return get_system_robot()


def _advance_to_next(proposal, from_instance, acting_user=None):
    """Advance the proposal past ``from_instance`` to the next stage.

    Returns the first newly active step instance, or None if the workflow
    terminated (proposal accepted). Reaching the terminal step both accepts
    the proposal *and* provisions it (project + resources), so the workflow
    engine and the legacy ``approve`` action converge on the same side
    effects. ``allocate_proposal`` is not idempotent — it always creates a
    Project — so allocation is guarded on the proposal not already having one.
    ``approved_by`` is stamped unconditionally (not only inside the allocation
    branch) so acceptance always records who decided it, even when a project
    already exists and allocation is skipped (``_award_approver``).
    """
    members = _next_stage(proposal, from_instance)
    if not members and from_instance is not None and from_instance.run_id:
        # The end of a post-award run: granted, or the report accepted.
        # Nothing is allocated; the proposal is accepted already.
        runs.close_run(
            proposal,
            from_instance.run,
            ProposalRunStates.APPROVED,
            acting_user or get_system_robot(),
        )
        return None
    if not members:
        approver = _award_approver(proposal, acting_user)
        collector = dry_run_context.current()
        if collector is not None:
            # A dry run provisions nothing: it tells what would be.
            collector.allocation(proposal)
        elif proposal.project_id is None:
            utils.allocate_proposal(proposal, approved_by=approver)
        proposal.approved_by = approver
        proposal.state = ProposalStates.ACCEPTED
        proposal.current_step = None
        proposal.save(update_fields=["state", "current_step", "approved_by"])
        return None

    return _enter_stage(proposal, members)


def automatic_decision_refusal(proposal, instance):
    """Why the proposal's automatic decision cannot be taken by the system,
    or None when it can.

    The proposal must have passed a step that could have rejected it, and
    that step must have been decided: a lapsed one passes nothing. The step
    is checked on the proposal's own path, so a call reconfigured after
    activation cannot let a proposal award itself.

    A decision relying on the provider fulfilment needs no such step: a
    person at the provider must act on every order the award places, on
    every offering the call has accepted by now
    (``utils.provider_fulfilment_refusal``).

    Either way, the limits of every item of the award must be ones
    allocation can order (``utils.unorderable_award``, which prefills the
    award if the proposal has none yet): the system does not accept a
    proposal whose project would be left without its resource.
    """
    if instance.call_step.relies_on_provider_fulfilment:
        reason = utils.provider_fulfilment_refusal(proposal.round.call)
    else:
        reason = _earlier_step_refusal(proposal, instance)
    return reason or utils.unorderable_award(proposal)


def _earlier_step_refusal(proposal, instance):
    """Why no decided step before ``instance`` could have rejected the
    proposal, or None."""
    gates = [
        earlier
        for earlier in _ordered_instances(proposal).filter(
            Q(order__lt=instance.order) | Q(order=instance.order, id__lt=instance.pk)
        )
        if earlier.status != WorkflowStepInstanceStatuses.SKIPPED
        and earlier.call_step.can_end_negatively
    ]
    if not gates:
        return "no earlier step could reject the proposal."
    lapsed = [
        _step_label(gate)
        for gate in gates
        if gate.status != WorkflowStepInstanceStatuses.COMPLETED
    ]
    if lapsed:
        return f"{', '.join(lapsed)} ended without a result."
    return None


def _decide_automatically(proposal, instance):
    """Approve the allocation decision ``instance`` as the system.

    The award is the one requested, prefilled on activation. When the
    approval is refused -- a gate such as a score threshold or a hard budget,
    amounts missing from the award, or no decided step before it that could
    reject the proposal -- the step stays active for its completer, the call
    managers, and the reason goes on the proposal's feed for the call team.
    Returns what completing the step returns: the next active step, or None
    when the proposal was accepted or the decision is held; on a refusal, the
    decision step itself. The caller holds the proposal's row lock inside a
    transaction.
    """
    reason = automatic_decision_refusal(proposal, instance)
    if reason is None:
        try:
            # Nobody in particular took the decision: the step's events name
            # no actor, whoever's request brought the proposal here.
            with transaction.atomic(), activity.without_actor():
                return complete_step(
                    proposal=proposal,
                    current_instance=instance,
                    outcome=WorkflowStepOutcomes.APPROVED,
                    outcome_reason="",
                    completed_by=get_system_robot(),
                )
        except ValueError as exc:
            reason = str(exc)
    logger.info(
        "Automatic decision on proposal %s left to the call managers: %s",
        proposal.uuid.hex,
        reason,
    )
    activity.log_automatic_decision_deferred(proposal, reason)
    return instance


def is_awaiting_manual_advance(proposal) -> bool:
    """True if the proposal's current step is COMPLETED with transition_mode=manual.

    ProposalViewSet.get_queryset replicates this predicate as queryset
    annotations to avoid an N+1 on list; keep the two in sync.
    """
    if not proposal.current_step_id or proposal.decision_held:
        return False
    instance = (
        models.ProposalWorkflowStepInstance.objects.select_related("call_step")
        .filter(pk=proposal.current_step_id)
        .first()
    )
    if not instance or instance.status != WorkflowStepInstanceStatuses.COMPLETED:
        return False
    return instance.call_step.transition_mode == TransitionModes.MANUAL


@transaction.atomic
def advance_step(proposal, acting_user=None):
    """Manually advance a workflow parked on a manual-mode completed step.

    Returns the newly active step instance, or None if the workflow
    terminated. Raises ValueError if the proposal is not in a state where
    manual advance is valid. ``acting_user`` is the call manager confirming the
    advance; if this advance reaches the terminal (allocation) step, the
    approver recorded is the allocation decision's completer, or the advancer
    when there is none (``_award_approver``).
    """
    if proposal.decision_held:
        raise ValueError(
            "The decision is held until the round's results are published."
        )
    if not is_awaiting_manual_advance(proposal):
        raise ValueError("Workflow is not awaiting manual advance.")
    return _advance_to_next(proposal, proposal.current_step, acting_user=acting_user)


@transaction.atomic
def reject_at_step(proposal, current_instance, reason, completed_by, internal_notes=""):
    """Mark an active step as completed with rejection and reject the proposal.

    Steps still running beside it in its stage are withdrawn.

    ``internal_notes`` is appended to any pre-existing internal notes on the
    instance; see ``complete_step`` for the rationale.
    """
    current_instance.status = WorkflowStepInstanceStatuses.COMPLETED
    current_instance.outcome = WorkflowStepOutcomes.REJECTED
    current_instance.outcome_reason = reason
    current_instance.completed_at = timezone.now()
    current_instance.completed_by = completed_by
    if internal_notes:
        current_instance.internal_notes = _merge_notes(
            current_instance.internal_notes, internal_notes
        )
    current_instance.save(
        update_fields=[
            "status",
            "outcome",
            "outcome_reason",
            "internal_notes",
            "completed_at",
            "completed_by",
        ]
    )
    # Work still open when the step ends is no longer wanted, nor are the
    # changes asked for on it.
    contributions.withdraw_open(current_instance)
    change_requests.close_open_of_step(
        current_instance, ChangeRequestResolutions.CANCELLED, completed_by
    )
    if _decision_is_withheld(proposal, current_instance):
        _hold_decision(proposal)
        return
    _announce(current_instance, NotificationRuleTriggers.STEP_REJECTED)
    _withdraw_siblings(proposal, current_instance)
    if current_instance.run_id:
        # Rejected at a post-award step: its run closes, nothing is granted,
        # and the proposal stays accepted.
        runs.close_run(
            proposal, current_instance.run, ProposalRunStates.REJECTED, completed_by
        )
        return
    proposal.state = ProposalStates.REJECTED
    proposal.current_step = None
    proposal.save(update_fields=["state", "current_step"])


def _enter_parked_step(proposal):
    """Start the step a proposal parked on a completed step waits to enter.

    Only so that a rejection can be recorded on it: no start notification is
    sent, the step ends as it begins. Returns the started instance, or None
    when no step follows.
    """
    members = _next_stage(proposal, proposal.current_step)
    if not members:
        return None
    next_instance = members[0]
    instance = (
        proposal.workflow_step_instances.select_for_update()
        .select_related("call_step")
        .get(pk=next_instance.pk)
    )
    instance.status = WorkflowStepInstanceStatuses.ACTIVE
    instance.started_at = timezone.now()
    instance.save(update_fields=["status", "started_at"])
    proposal.current_step = instance
    proposal.save(update_fields=["current_step"])
    return instance


UNDECIDED_AT_COMPLETION_REASON = "Not decided when the round was completed."


@transaction.atomic
def reject_undecided(proposal, completed_by):
    """Reject a proposal left without a decision when its round is completed.

    The rejection is recorded on the step the proposal stands in, as if the
    step's actor had rejected it, and sends that step's rejection
    notifications. A proposal parked on a completed step, waiting to be moved
    on by hand, stands in the step it was waiting to enter: that step starts
    and is rejected at once, the completed one keeps its outcome. A proposal
    whose workflow has not started yet is rejected outright. Returns the
    state the proposal was in. The caller holds the proposal's row lock, then
    the round's, inside a transaction, and the round's results are published,
    so nothing is held back.
    """
    previous_state = proposal.state
    instance = (
        proposal.workflow_step_instances.select_for_update()
        .select_related("call_step")
        .filter(
            pk=proposal.current_step_id,
            status=WorkflowStepInstanceStatuses.ACTIVE,
        )
        .first()
        if proposal.current_step_id
        else None
    )
    if instance is None and proposal.current_step_id:
        instance = _enter_parked_step(proposal)
    if instance is not None:
        reject_at_step(proposal, instance, UNDECIDED_AT_COMPLETION_REASON, completed_by)
        if proposal.decision_held:
            raise ValueError("The round still holds its decisions back.")
    else:
        proposal.state = ProposalStates.REJECTED
        proposal.current_step = None
        proposal.save(update_fields=["state", "current_step"])

    event_logger.emit(
        activity.worded(
            "Proposal {proposal_name} was rejected by {actor}: it had no "
            "decision when its round was completed."
        ),
        event_type=EventType.PROPOSAL_REJECTED_AT_ROUND_COMPLETION,
        event_context={
            "proposal": proposal,
            "actor": completed_by.full_name or completed_by.username,
            "rejected_at_step": instance.step if instance else None,
            "previous_state": previous_state,
        },
        scopes=[proposal],
    )
    return previous_state


@transaction.atomic
def expire_step(current_instance):
    """Mark an active step as expired and advance the workflow.

    If a next enabled step exists, activate it and return the new instance.
    If no next step exists (terminal expiry), reject the proposal and return None.
    Returns a tuple (next_instance, proposal_was_rejected).
    """
    proposal = current_instance.proposal
    if is_final_report(current_instance):
        return _mark_overdue(current_instance)
    if current_instance.responds_to_inputs:
        return _complete_without_response(proposal, current_instance)
    current_instance.status = WorkflowStepInstanceStatuses.EXPIRED
    current_instance.outcome = WorkflowStepOutcomes.EXPIRED
    current_instance.completed_at = timezone.now()
    current_instance.save(update_fields=["status", "outcome", "completed_at"])
    contributions.expire_open(current_instance)
    if _decision_is_withheld(proposal, current_instance):
        # A lapsed decision is an outcome like any other: whether it ends the
        # workflow or moves on to the award response, the move and the
        # expiry notice both tell the applicant, so both wait for the round.
        _hold_decision(proposal)
        return None, False
    _announce(current_instance, NotificationRuleTriggers.STEP_EXPIRED)
    return _continue_after_expiry(proposal, current_instance)


def is_final_report(instance) -> bool:
    """Whether ``instance`` is the applicant's final report: the applicant
    step of a closing run."""
    return bool(
        instance.run_id
        and instance.call_step.phase == WorkflowPhases.CLOSING
        and instance.call_step.kind == StepKinds.APPLICANT_ACTION
    )


def is_overdue(instance) -> bool:
    """Whether an active step is past its deadline and still waits: only a
    final report does, the rest lapse."""
    return bool(
        instance.status == WorkflowStepInstanceStatuses.ACTIVE
        and instance.deadline is not None
        and instance.deadline < timezone.now()
    )


def _mark_overdue(instance):
    """A final report past its due date stays to be submitted: the report is
    still owed. Its lapse is told once, through the step's expiry rules, and
    the sweep leaves the step alone afterwards (``OVERDUE_LEDGER_KEY``)."""
    _announce(instance, NotificationRuleTriggers.STEP_EXPIRED)
    instance.refresh_from_db(fields=["sent_notifications"])
    if OVERDUE_LEDGER_KEY not in instance.sent_notifications:
        instance.sent_notifications = [
            *instance.sent_notifications,
            OVERDUE_LEDGER_KEY,
        ]
        instance.save(update_fields=["sent_notifications"])
    return None, False


# Recorded on a final report once its lapse has been told.
OVERDUE_LEDGER_KEY = "overdue"


def _complete_without_response(proposal, instance):
    """Close a lapsed response step as answered with nothing.

    The applicant chose not to respond: the step is completed with the
    ``no_response`` outcome and the workflow goes on to the next step, as it
    would after a response. Whatever the applicant left unsubmitted stays
    unread. The step's completion rules are notified, not its expiry ones:
    nothing went wrong. A call cannot be configured with a response step no
    decision follows; should one be reached at the end of the path anyway,
    the workflow ends as after any lapsed last step, without an award.
    """
    instance.status = WorkflowStepInstanceStatuses.COMPLETED
    instance.outcome = WorkflowStepOutcomes.NO_RESPONSE
    instance.completed_at = timezone.now()
    instance.save(update_fields=["status", "outcome", "completed_at"])
    contributions.expire_open(instance)
    _announce(instance, NotificationRuleTriggers.STEP_COMPLETED)
    if instance.call_step.transition_mode == TransitionModes.MANUAL:
        # Parked like any manually advanced step, for the call manager.
        return None, False
    return _continue_after_expiry(proposal, instance)


def _continue_after_expiry(proposal, instance):
    """Move past a lapsed step: on to the next stage, or reject at the end.

    Steps of its stage still running hold the proposal where it is, and a
    step of it completed for the call managers to advance parks it there.

    In a post-award run, an extension request the applicant let lapse
    withdraws the run, and a run left with no step to go on to ends expired:
    nothing is granted, and the proposal stays accepted.
    """
    if (
        instance.run_id
        and instance.call_step.kind == StepKinds.APPLICANT_ACTION
        and instance.status == WorkflowStepInstanceStatuses.EXPIRED
    ):
        _withdraw_siblings(proposal, instance)
        runs.close_run(proposal, instance.run, ProposalRunStates.WITHDRAWN, None)
        return None, False
    if _stage_still_running(proposal, instance):
        return None, False
    parked = _parking_member(proposal, instance)
    if parked is not None:
        if proposal.current_step_id != parked.pk:
            proposal.current_step = parked
            proposal.save(update_fields=["current_step"])
        return None, False
    members = _next_stage(proposal, instance)
    if not members and instance.run_id:
        runs.close_run(proposal, instance.run, ProposalRunStates.EXPIRED, None)
        return None, False
    if not members:
        proposal.state = ProposalStates.REJECTED
        proposal.current_step = None
        proposal.save(update_fields=["state", "current_step"])
        return None, True

    return _enter_stage(proposal, members), False


def release_held_decision(proposal):
    """Carry out a held allocation decision as if it had just been made.

    Negative outcomes (declined, rejected) reject the proposal; a lapse goes
    where an unheld lapse goes -- the next step, or a rejection at the end;
    an approval continues to the next step (the award response) or, at the
    end of the workflow, accepts and provisions it. Every notification the
    decision would have sent -- the expiry notice included -- is sent here.
    Publication is the call manager's confirmation, so a manual transition
    mode does not park it again. Returns the proposal's new state when the
    workflow ended, None when it continues to another step. The caller holds
    the proposal's row lock inside a transaction.
    """
    # The decision held is the step the proposal stands in: the hold keeps it
    # there until publication.
    instance = proposal.current_step or allocation_decision_instance(proposal)
    proposal.decision_held = False
    if instance.outcome == WorkflowStepOutcomes.EXPIRED:
        proposal.save(update_fields=["decision_held"])
        _announce(instance, NotificationRuleTriggers.STEP_EXPIRED)
        next_instance, _ = _continue_after_expiry(proposal, instance)
        return proposal.state if next_instance is None else None
    if instance.outcome in (
        WorkflowStepOutcomes.NEGATIVE_OUTCOMES | {WorkflowStepOutcomes.REJECTED}
    ):
        _announce(instance, NotificationRuleTriggers.STEP_REJECTED)
        proposal.state = ProposalStates.REJECTED
        proposal.current_step = None
        proposal.save(update_fields=["decision_held", "state", "current_step"])
        return proposal.state

    proposal.save(update_fields=["decision_held"])
    _announce(instance, NotificationRuleTriggers.STEP_COMPLETED)
    next_instance = _advance_to_next(
        proposal, instance, acting_user=instance.completed_by
    )
    return proposal.state if next_instance is None else None


class DecisionNotReopenable(ValueError):
    """The proposal has no held decision that can still be taken back."""


@transaction.atomic
def reopen_held_decision(proposal, user, reason, deadline=None):
    """Take a held allocation decision back so it can be made again.

    Only while the round withholds its results: once they are published the
    decision has been (or is being) announced. The decision step becomes
    active again with its recorded outcome cleared, and the proposal counts
    as undecided for the round's publication. The deadline is ``deadline``
    when given; otherwise one that has already passed is dropped, so the
    step does not lapse again at the next sweep, and one still ahead is kept.
    Nothing is sent to anyone: the outcome was never announced. The reopen
    is logged on the proposal's feed without the reason or the outcome it
    undid: the applicant team reads that feed, and both would tell them the
    decision. Those two go to the server log only. Returns the reactivated
    step instance.

    Locks the proposal, then the round, the order every decision and the
    round's publication take.
    """
    proposal = (
        models.Proposal.objects.select_for_update(of=("self",))
        .select_related("round__call__manager__customer")
        .get(pk=proposal.pk)
    )
    if not proposal.decision_held or not (
        proposal.current_step_id and proposal.current_step.is_allocation_decision
    ):
        raise DecisionNotReopenable("The proposal has no held decision to reopen.")
    call_round = models.Round.objects.select_for_update().get(pk=proposal.round_id)
    call_round.call = proposal.round.call
    if not call_round.withholds_results:
        raise DecisionNotReopenable(
            "The round's results are published; the decision can no longer be reopened."
        )
    if call_round.call.adoption_required and call_round.adopted_at is not None:
        raise DecisionNotReopenable(
            "The round's results have been adopted; the decision can no longer "
            "be reopened."
        )
    instance = proposal.workflow_step_instances.select_for_update().get(
        pk=proposal.current_step_id
    )
    previous_outcome = instance.outcome

    instance.status = WorkflowStepInstanceStatuses.ACTIVE
    instance.outcome = None
    instance.outcome_reason = ""
    instance.completed_at = None
    instance.completed_by = None
    if deadline is not None:
        instance.deadline = deadline
    elif instance.deadline is not None and instance.deadline <= timezone.now():
        instance.deadline = None
    instance.save(
        update_fields=[
            "status",
            "outcome",
            "outcome_reason",
            "completed_at",
            "completed_by",
            "deadline",
        ]
    )
    proposal.decision_held = False
    proposal.save(update_fields=["decision_held"])

    # Filed on the proposal's feed alone. Its scope guard keeps evaluators out
    # but lets the applicant team in, so the event names neither the outcome
    # taken back nor the free-text reason, which may well state it. The
    # managing organisation's feed is no place for it either: its owners need
    # not manage the call, and an applicant may be one of them.
    event_logger.emit(
        activity.worded(
            "Allocation decision on proposal {proposal_name} was reopened by {actor}."
        ),
        event_type=EventType.PROPOSAL_DECISION_REOPENED,
        event_context={
            "proposal": proposal,
            "actor": user.full_name or user.username,
            "deadline": instance.deadline.isoformat() if instance.deadline else None,
        },
        scopes=[proposal],
    )
    logger.info(
        "Held decision on proposal %s reopened by user %s; previous outcome %s; "
        "reason: %s",
        proposal.uuid.hex,
        user.uuid.hex,
        previous_outcome,
        reason,
    )
    return instance


# =============================================================================
# Requests for changes
# =============================================================================


class ChangeRequestError(ValueError):
    """A request for changes that cannot be made, submitted or cancelled."""


def open_change_request(instance):
    """The open request for changes to the applicant made at ``instance``."""
    return (
        change_requests.open_requests()
        .filter(step_instance=instance, contribution__isnull=True)
        .first()
    )


def _restart_clock(instance):
    """Give the step's actor its full duration again from now: the time the
    step waited on someone else is not theirs."""
    duration = instance.call_step.duration_in_days
    if not duration:
        return
    instance.deadline = timezone.now() + timedelta(days=duration)
    instance.save(update_fields=["deadline"])


def _announce_change(change_request, trigger):
    """Tell of a request for changes being made or closed: mail to whom it
    concerns, the step's notification rules for ``trigger``, and the
    proposal's activity."""
    notification_rules.dispatch_change_request(change_request, trigger)
    activity.log_change_request(change_request, trigger)


def request_changes(proposal, instance, user, message, parts, due_at=None):
    """Send ``proposal`` back to its applicant to change ``parts``.

    The proposal stays ``in_review`` and the step active; the parts listed
    become editable for the applicant until the changes are submitted
    (``change_requests.applicant_may_edit``). The step does not lapse
    meanwhile. The caller holds the proposal's row lock, then the step's,
    inside a transaction. Returns the request.
    """
    call_step = instance.call_step
    if instance.run_id:
        # At a step of a post-award run: the run's own parts, while it runs.
        if proposal.state != ProposalStates.ACCEPTED or not instance.run.is_open:
            raise ChangeRequestError(
                "Changes are requested while the extension request or final "
                "report is open."
            )
    elif proposal.state != ProposalStates.IN_REVIEW:
        raise ChangeRequestError("Changes are requested of a proposal in review.")
    if instance.status != WorkflowStepInstanceStatuses.ACTIVE:
        raise ChangeRequestError(
            f"The step {_step_label(instance)} is {instance.status}: changes are "
            "requested at an active step."
        )
    if not call_step.allows_change_requests or instance.is_allocation_decision:
        raise ChangeRequestError(
            f"The step {_step_label(instance)} does not allow requesting changes."
        )
    if open_change_request(instance) is not None:
        raise ChangeRequestError(
            "Changes have already been requested at this step and not yet submitted."
        )
    parts = list(dict.fromkeys(parts))
    if not parts:
        raise ChangeRequestError("Name at least one part the applicant may change.")
    allowed = change_requests.unlockable_parts(proposal, instance)
    unknown = [part for part in parts if part not in allowed]
    if unknown:
        raise ChangeRequestError(
            f"These parts cannot be unlocked on this proposal: {', '.join(unknown)}. "
            f"Choose from: {', '.join(allowed)}."
        )
    if due_at is not None and due_at <= timezone.now():
        raise ChangeRequestError("The due date must be in the future.")
    change_request = models.ProposalChangeRequest.objects.create(
        proposal=proposal,
        step_instance=instance,
        requested_by=user,
        message=message,
        unlocked_parts=parts,
        due_at=due_at,
    )
    _announce_change(change_request, NotificationRuleTriggers.CHANGES_REQUESTED)
    return change_request


def submit_changes(proposal, user):
    """Close every open request for changes on ``proposal`` as submitted.

    Several steps of a stage may each have asked; the applicant answers
    them at once. Each asking step goes back to its actor with its full
    duration again; whom the requests concern is told once, of every
    request that concerns them. The proposal's submission time is
    left as it is: it was submitted when it left draft, whatever was
    changed since. The caller holds the proposal's row lock inside a
    transaction and has checked the proposal is complete. Returns the
    closed requests, earliest first.
    """
    pending = list(change_requests.open_applicant_requests(proposal))
    if not pending:
        raise ChangeRequestError("No changes have been requested of this proposal.")
    # The lock order the step actions take: proposal, steps, requests.
    instances = {
        instance.pk: instance
        for instance in models.ProposalWorkflowStepInstance.objects.select_for_update(
            of=("self",)
        )
        .select_related("call_step")
        .filter(pk__in=[request.step_instance_id for request in pending])
        .order_by("pk")
    }
    closed = []
    for change_request in models.ProposalChangeRequest.objects.select_for_update(
        of=("self",)
    ).filter(pk__in=[request.pk for request in pending], closed_at__isnull=True):
        change_requests.close(change_request, ChangeRequestResolutions.SUBMITTED, user)
        change_request.step_instance = instances[change_request.step_instance_id]
        change_request.proposal = proposal
        closed.append(change_request)
    if not closed:
        raise ChangeRequestError("No changes have been requested of this proposal.")
    closed.sort(key=lambda change_request: (change_request.created, change_request.pk))
    for change_request in closed:
        _restart_clock(change_request.step_instance)
    notification_rules.dispatch_changes_submitted(closed)
    activity.log_changes_submitted(proposal, closed)
    return closed


def cancel_change_request(proposal, change_request, user):
    """Call off an open request for changes to the applicant: the step is
    its actor's again, with its full duration. The caller holds the
    proposal's row lock inside a transaction."""
    change_request = models.ProposalChangeRequest.objects.select_for_update().get(
        pk=change_request.pk
    )
    if not change_request.is_open:
        raise ChangeRequestError("The request for changes is already closed.")
    if change_request.contribution_id is not None:
        raise ChangeRequestError(
            "A reopened contribution is withdrawn from the step's work instead."
        )
    instance = models.ProposalWorkflowStepInstance.objects.select_for_update(
        of=("self",)
    ).get(pk=change_request.step_instance_id)
    change_requests.close(change_request, ChangeRequestResolutions.CANCELLED, user)
    if instance.status == WorkflowStepInstanceStatuses.ACTIVE:
        _restart_clock(instance)
    activity.log_change_request(change_request, None)
    return change_request


def lapse_change_request(change_request):
    """Close a request for changes whose due date passed without them.

    The step returns to its actor, with its full duration, and they are
    told; nothing is decided for them. The caller holds the proposal's row
    lock, then the step's, inside a transaction.
    """
    change_requests.close(change_request, ChangeRequestResolutions.LAPSED)
    instance = change_request.step_instance
    if instance.status == WorkflowStepInstanceStatuses.ACTIVE:
        _restart_clock(instance)
    notification_rules.dispatch_change_request(change_request, None)
    activity.log_change_request(change_request, None)
    return change_request


def may_reopen_step_work(instance) -> bool:
    """Whether the work submitted to ``instance`` may still be reopened:
    while the step is active, or completed with the proposal still in its
    stage -- another step of it running, or the stage parked for a manual
    advance -- not once the proposal has moved on."""
    if instance.status == WorkflowStepInstanceStatuses.ACTIVE:
        return True
    if instance.status != WorkflowStepInstanceStatuses.COMPLETED:
        return False
    proposal = instance.proposal
    current = proposal.current_step
    if (
        current is None
        or not _stage_members(proposal, current).filter(pk=instance.pk).exists()
    ):
        return False
    if current.status == WorkflowStepInstanceStatuses.ACTIVE:
        return True
    return is_awaiting_manual_advance(proposal)


def _reactivate(proposal, instance):
    """Make a completed step of the stage the proposal stands in active
    again, its outcome cleared, as ``reopen_held_decision`` does for a held
    decision. Its completion is announced again when it completes again.

    A proposal parked for a manual advance now stands in the step: it is no
    longer waiting to be moved on, and completing the step parks it again
    (``complete_step``), so the next stage starts once.
    """
    instance.status = WorkflowStepInstanceStatuses.ACTIVE
    instance.outcome = None
    instance.outcome_reason = ""
    instance.feedback_withheld = False
    instance.completed_at = None
    instance.completed_by = None
    if instance.deadline is not None and instance.deadline <= timezone.now():
        duration = instance.call_step.duration_in_days
        instance.deadline = (
            timezone.now() + timedelta(days=duration) if duration else None
        )
    instance.sent_notifications = [
        key
        for key in instance.sent_notifications
        if key
        not in (
            NotificationRuleTriggers.STEP_COMPLETED,
            NotificationRuleTriggers.STEP_REJECTED,
        )
    ]
    instance.save(
        update_fields=[
            "status",
            "outcome",
            "outcome_reason",
            "feedback_withheld",
            "completed_at",
            "completed_by",
            "deadline",
            "sent_notifications",
        ]
    )
    if proposal.current_step_id != instance.pk and not (
        active_instances(proposal).exclude(pk=instance.pk).exists()
    ):
        proposal.current_step = instance
        proposal.save(update_fields=["current_step"])


def reopen_contribution(proposal, instance, contribution, user, message):
    """Hand a submitted contribution back to its contributor to submit again.

    Its answers are kept, so the contributor revises rather than retypes;
    its outcome and conditions are cleared, and it counts again only once
    resubmitted. A slot handed to someone who took it over and may still
    hold it goes back to them, already accepted. Allowed while the step is active, or completed with the
    proposal parked on it for a manual advance, in which case the step
    becomes active again. Not for a review, which keeps its own states, nor
    for an applicant's form. The caller holds the proposal's, the step's and
    the contribution's row locks, in that order, inside a transaction.
    Returns the request recording the reopening.
    """
    call_step = instance.call_step
    if contribution.status != StepContributionStatuses.SUBMITTED:
        raise ChangeRequestError(
            f"The contribution is {contribution.status}: only a submitted one "
            "is reopened."
        )
    if contribution.review_id is not None:
        raise ChangeRequestError(
            "A review is not reopened here: it keeps its own states."
        )
    if (
        call_step.kind == StepKinds.APPLICANT_ACTION
        or not call_step.collects_contributions
        or instance.is_allocation_decision
    ):
        raise ChangeRequestError(
            f"The step {_step_label(instance)} takes no contributions to reopen."
        )
    if proposal.state != ProposalStates.IN_REVIEW or proposal.decision_held:
        raise ChangeRequestError("Contributions are reopened while in review.")
    if not may_reopen_step_work(instance):
        raise ChangeRequestError(
            f"The step {_step_label(instance)} is over and the proposal has "
            "moved on: its contributions can no longer be reopened."
        )
    if instance.status != WorkflowStepInstanceStatuses.ACTIVE:
        _reactivate(proposal, instance)
    contribution.proposal_step = instance
    # A slot handed to someone who took it over goes back to them, accepted:
    # they wrote it and revise it. Else to its holders.
    contribution.status = (
        StepContributionStatuses.ACCEPTED
        if contributions.effective_delegate_id(contribution) is not None
        else StepContributionStatuses.PENDING
    )
    contribution.outcome = ""
    contribution.conditions = ""
    contribution.submitted_at = None
    contribution.save(
        update_fields=["status", "outcome", "conditions", "submitted_at", "modified"]
    )
    change_request = models.ProposalChangeRequest.objects.create(
        proposal=proposal,
        step_instance=instance,
        contribution=contribution,
        requested_by=user,
        message=message,
    )
    _announce_change(change_request, NotificationRuleTriggers.CONTRIBUTION_REOPENED)
    return change_request
