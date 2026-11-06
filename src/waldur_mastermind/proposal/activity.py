"""A proposal's and a review's activity: what is logged about them, and who
reads which part of it.

Workflow transitions are logged on the proposal's own feed, review events on
the review's feed and the proposal's. Neither is filed on a wider feed (the
managing organisation's): their readers are decided here, per viewer.

On a proposal's feed (``proposal_event_rows``):

- staff, support and the call team -- whoever may update the call, through
  the call or its managing organisation, and is not on the proposal's
  applicant team -- read everything;
- the applicant team reads the applicant-safe events, ``APPLICANT_EVENT_TYPES``,
  and its own actions. These events are done by the applicant team, the call
  team or nobody, so no evaluator is named in them; the outcome events that
  follow an evaluator's work are logged without an actor;
- anyone else who reads the proposal -- its rapporteurs, panel members,
  reviewers, providers -- reads only what they did themselves.

The event-type guards still apply on top: a report release is for the call
team, and a held decision, its reopening included, for those who may see it.

A review's feed is read by staff, support, the reviewer and the call team
(``can_view_review_event_feed``); its events never name the reviewer.
"""

from contextlib import contextmanager

from django.db.models import Q

from waldur_core.core.service_access import names_calls
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.logging.middleware import (
    get_event_context,
    reset_event_context,
    set_event_context,
)
from waldur_mastermind.proposal import permissions, proposal_roles
from waldur_mastermind.proposal.enums import (
    NotificationRuleTriggers,
    StepKinds,
    WorkflowPhases,
)

# What the applicant team reads on its proposal's feed besides its own
# actions: its own work, the team's changes, and the outcomes it is told of.
APPLICANT_EVENT_TYPES = frozenset(
    {
        EventType.PROPOSAL_CREATED,
        EventType.PROPOSAL_SUBMITTED,
        EventType.PROPOSAL_CANCELED,
        EventType.PROPOSAL_CHANGES_REQUESTED,
        EventType.PROPOSAL_CHANGES_SUBMITTED,
        EventType.PROPOSAL_CHANGE_REQUEST_CLOSED,
        EventType.PROPOSAL_DOCUMENT_ADDED,
        EventType.PROPOSAL_DOCUMENT_REMOVED,
        EventType.PROPOSAL_DRAFT_CARRIED_OVER,
        EventType.PROPOSAL_RUN_OPENED,
        EventType.PROPOSAL_RUN_CLOSED,
        EventType.PROPOSAL_DECISION_PUBLISHED,
        EventType.PROPOSAL_DECISION_REOPENED,
        EventType.PROPOSAL_REJECTED_AT_ROUND_COMPLETION,
        EventType.PROPOSAL_APPLICANT_RESPONDED,
        EventType.PROPOSAL_EDITED_FOR_APPLICANT,
        # A message the call wrote to them.
        EventType.PROPOSAL_NOTIFICATION_SENT,
        EventType.ROLE_GRANTED,
        EventType.ROLE_REVOKED,
        EventType.ROLE_UPDATED,
        EventType.USER_INVITATION_UPDATED,
        EventType.USER_INVITATION_DELETED,
        EventType.USER_GROUP_INVITATION_UPDATED,
    }
)


def _own_actions(user) -> Q:
    return Q(context__user_uuid=user.uuid.hex)


def proposal_event_rows(user, proposal) -> Q | None:
    """The events of ``proposal``'s feed ``user`` reads; ``None`` for all."""
    if user.is_staff or user.is_support:
        return None
    if proposal_roles.is_on_applicant_team(user, proposal):
        return Q(event_type__in=APPLICANT_EVENT_TYPES) | _own_actions(user)
    if permissions.oversees_proposal_call(user, proposal):
        return None
    return _own_actions(user)


def can_view_review_event_feed(user, review) -> bool:
    """Whether ``user`` reads the events logged against ``review``.

    The reviewer and those overseeing the call do; the applicant team does
    not, even where the call shows it the review, because every event's
    context carries the acting user, and the reviewer acts on most of them.
    """
    if user.is_staff or user.is_support or review.reviewer_id == user.id:
        return True
    proposal = review.proposal
    return permissions.oversees_proposal_call(
        user, proposal
    ) and not proposal_roles.is_on_applicant_team(user, proposal)


@contextmanager
def without_actor():
    """Log an event as no one's: what the applicant is told of an outcome
    names nobody, though an evaluator's request brought it about."""
    saved = get_event_context()
    reset_event_context()
    try:
        yield
    finally:
        if saved is not None:
            set_event_context(saved)


def worded(message):
    """``message`` in the deployment's vocabulary.

    A marketplace-only deployment calls its applicants' proposals access
    requests and never shows them a call (``names_calls``). The feed is the
    applicant's as well as the call team's, so its messages are written that
    way when they are logged.
    """
    if names_calls():
        return message
    return message.replace(
        "Proposal {proposal_name}", "Access request {proposal_name}"
    ).replace("proposal {proposal_name}", "access request {proposal_name}")


def _emit(message, event_type, proposal, scopes=None, **context):
    event_logger.emit(
        worded(message),
        event_type=event_type,
        event_context={"proposal": proposal, **context},
        scopes=scopes or [proposal],
    )


def _step_name(instance):
    return instance.call_step.name or instance.step


def log_step_event(instance, trigger):
    """Log a step's start or end on its proposal's feed.

    Called where the step's notifications are dispatched, so an allocation
    decision held for its round is logged when the round publishes it.
    """
    proposal = instance.proposal
    step_name = _step_name(instance)
    if trigger == NotificationRuleTriggers.STEP_STARTED:
        with without_actor():
            _emit(
                "Step {step_name} of proposal {proposal_name} started.",
                EventType.PROPOSAL_STEP_STARTED,
                proposal,
                step_name=step_name,
            )
        return
    if trigger not in (
        NotificationRuleTriggers.STEP_COMPLETED,
        NotificationRuleTriggers.STEP_REJECTED,
        NotificationRuleTriggers.STEP_EXPIRED,
    ):
        return
    outcome = instance.outcome or ""
    if instance.call_step.kind == StepKinds.APPLICANT_ACTION:
        _log_applicant_answer(instance, trigger, step_name, outcome)
    else:
        _emit(
            "Step {step_name} of proposal {proposal_name} was completed: {outcome}.",
            EventType.PROPOSAL_STEP_COMPLETED,
            proposal,
            step_name=step_name,
            outcome=outcome,
        )
    if instance.is_allocation_decision:
        with without_actor():
            _emit(
                "Allocation decision on proposal {proposal_name} was "
                "published: {outcome}.",
                EventType.PROPOSAL_DECISION_PUBLISHED,
                proposal,
                step_name=step_name,
                outcome=outcome,
            )


def _log_applicant_answer(instance, trigger, step_name, outcome):
    """An applicant step ended: answered by the applicant, answered for them
    by someone else -- named, with why -- or left to expire."""
    proposal = instance.proposal
    if trigger == NotificationRuleTriggers.STEP_EXPIRED:
        with without_actor():
            _emit(
                "Step {step_name} of proposal {proposal_name} expired without "
                "the applicant's answer.",
                EventType.PROPOSAL_APPLICANT_RESPONDED,
                proposal,
                step_name=step_name,
                outcome=outcome,
            )
        return
    actor = instance.completed_by
    if actor is not None and proposal_roles.answers_for_applicant(actor, instance):
        _emit(
            "{actor_name} answered step {step_name} of proposal {proposal_name} "
            "on behalf of the applicant: {outcome}. Reason: {reason}",
            EventType.PROPOSAL_APPLICANT_RESPONDED,
            proposal,
            step_name=step_name,
            outcome=outcome,
            actor_name=actor.full_name or actor.username,
            reason=instance.outcome_reason or "",
            on_behalf_of_applicant=True,
        )
        return
    if actor is not None and not proposal_roles.is_on_applicant_team(actor, proposal):
        # The call gives its own team this step of the applicant's kind.
        _emit(
            "{actor_name} completed step {step_name} of proposal "
            "{proposal_name}: {outcome}.",
            EventType.PROPOSAL_APPLICANT_RESPONDED,
            proposal,
            step_name=step_name,
            outcome=outcome,
            actor_name=actor.full_name or actor.username,
        )
        return
    _emit(
        "The applicant answered step {step_name} of proposal "
        "{proposal_name}: {outcome}.",
        EventType.PROPOSAL_APPLICANT_RESPONDED,
        proposal,
        step_name=step_name,
        outcome=outcome,
    )


def log_award_changed(proposal, change, item, before=None):
    """The call team changed the award, which starts as the applicant's
    request: logged for the call team, with what it was and what it is."""
    offering_name = item.requested_offering.offering.name if item else ""
    _emit(
        "The award of proposal {proposal_name} was changed: {summary}.",
        EventType.PROPOSAL_AWARD_CHANGED,
        proposal,
        change=change,
        summary=f"{change} {offering_name}".strip(),
        offering_name=offering_name,
        before=before or {},
        after=award_item_state(item) if item and change != "removed" else {},
    )


def award_item_state(item) -> dict:
    """What an award item grants, as the award change event records it."""
    plan = item.effective_plan
    return {
        "offering": item.requested_offering.offering.name,
        "plan": plan.name if plan else "",
        "limits": item.limits or {},
        "attributes": item.attributes or {},
        "description": item.description or "",
    }


def log_edited_for_applicant(proposal, what, changes=None):
    """Someone outside the applicant team changed what the applicant
    submitted: the applicant team reads who, and what."""
    _emit(
        "{what} of proposal {proposal_name} changed by someone outside the "
        "applicant team.",
        EventType.PROPOSAL_EDITED_FOR_APPLICANT,
        proposal,
        what=what,
        changes=changes or {},
    )


def log_step_skipped(instance):
    """Log a step the proposal skips because its run condition does not hold.

    Not for the applicant team: their feed tells them of outcomes, not of
    the steps their proposal did not need.
    """
    with without_actor():
        _emit(
            "Step {step_name} of proposal {proposal_name} was skipped: its run "
            "condition was not met.",
            EventType.PROPOSAL_STEP_SKIPPED,
            instance.proposal,
            step_name=_step_name(instance),
        )


def log_step_withdrawn(instance):
    """Log a step stopped because a step of its stage ended the workflow."""
    with without_actor():
        _emit(
            "Step {step_name} of proposal {proposal_name} was withdrawn: the "
            "workflow ended at a step running beside it.",
            EventType.PROPOSAL_STEP_WITHDRAWN,
            instance.proposal,
            step_name=_step_name(instance),
        )


_RUN_NAMES = {
    WorkflowPhases.EXTENSION: "Extension request",
    WorkflowPhases.CLOSING: "Final report",
}


def log_run_opened(run):
    """Log an extension request or final report opened on an accepted
    proposal. The applicant team reads it: the run is theirs to act on."""
    _emit(
        "{run_name} of proposal {proposal_name} was opened.",
        EventType.PROPOSAL_RUN_OPENED,
        run.proposal,
        run_name=_RUN_NAMES.get(run.phase, run.phase),
        run_phase=run.phase,
    )


def log_run_closed(run):
    """Log a post-award run's end and its outcome, without what was granted:
    the applicant team reads the outcome, and the grant on the run."""
    with without_actor():
        _emit(
            "{run_name} of proposal {proposal_name} was closed: {run_state}.",
            EventType.PROPOSAL_RUN_CLOSED,
            run.proposal,
            run_name=_RUN_NAMES.get(run.phase, run.phase),
            run_phase=run.phase,
            run_state=run.state,
        )


def log_decision_held(proposal):
    _emit(
        "Allocation decision on proposal {proposal_name} is held until its "
        "round publishes its results.",
        EventType.PROPOSAL_DECISION_HELD,
        proposal,
    )


def log_automatic_decision_deferred(proposal, reason):
    """The automatic allocation decision was not taken and waits for the call
    managers. Logged as no one's: the reason may name a score or a budget,
    and the evaluator whose work brought the proposal here is not to read it
    as their own action. The applicant team does not read this event."""
    with without_actor():
        _emit(
            "Automatic allocation decision on proposal {proposal_name} was not "
            "taken and is left to the call managers: {reason}",
            EventType.PROPOSAL_AUTOMATIC_DECISION_DEFERRED,
            proposal,
            reason=reason,
        )


def log_budget_overridden(proposal, exceeded, reason):
    """An award was approved past what remains of the call's budgets. The
    call team's to read: the applicant team does not read this event."""
    _emit(
        "Award of proposal {proposal_name} was approved past the call's "
        "budget ({exceeded}): {reason}",
        EventType.PROPOSAL_BUDGET_OVERRIDDEN,
        proposal,
        exceeded=exceeded,
        reason=reason,
    )


def log_created(proposal):
    _emit(
        "Proposal {proposal_name} was created.",
        EventType.PROPOSAL_CREATED,
        proposal,
    )


def log_submitted(proposal):
    _emit(
        "Proposal {proposal_name} was submitted.",
        EventType.PROPOSAL_SUBMITTED,
        proposal,
    )


def log_panel_changed(proposal, previous, user):
    """Log a proposal's panel: routed when its evaluation started (no
    ``user``), or moved by someone.

    Not for the applicant team: which panel evaluates the proposal is the
    call team's to know.
    """
    panel_name = proposal.panel.name if proposal.panel_id else "none"
    if user is None:
        with without_actor():
            _emit(
                "Proposal {proposal_name} was routed to panel {panel_name}.",
                EventType.PROPOSAL_PANEL_CHANGED,
                proposal,
                previous_panel_name="none",
                panel_name=panel_name,
            )
        return
    _emit(
        "Panel of proposal {proposal_name} was changed from {previous_panel_name} "
        "to {panel_name} by {actor}.",
        EventType.PROPOSAL_PANEL_CHANGED,
        proposal,
        previous_panel_name=previous.name if previous else "none",
        panel_name=panel_name,
        actor=user.full_name or user.username,
    )


def log_contribution_submitted(contribution):
    instance = contribution.proposal_step
    _emit(
        "A contribution to step {step_name} of proposal {proposal_name} was submitted.",
        EventType.PROPOSAL_CONTRIBUTION_SUBMITTED,
        instance.proposal,
        step_name=_step_name(instance),
        outcome=contribution.outcome or "",
    )


def _assignment_context(assignment):
    user = assignment.user
    return {
        "role_name": assignment.role.name,
        "evaluator_name": user.full_name or user.username,
        "affected_user_uuid": user.uuid.hex,
    }


def log_evaluator_assigned(assignment):
    _emit(
        "{evaluator_name} was assigned as {role_name} on proposal {proposal_name}.",
        EventType.PROPOSAL_EVALUATOR_ASSIGNED,
        assignment.proposal,
        **_assignment_context(assignment),
    )


def log_evaluator_revoked(assignment):
    _emit(
        "{evaluator_name} was removed as {role_name} from proposal {proposal_name}.",
        EventType.PROPOSAL_EVALUATOR_REVOKED,
        assignment.proposal,
        **_assignment_context(assignment),
    )


REVIEW_MESSAGES = {
    EventType.REVIEW_ASSIGNED: "A review of proposal {proposal_name} was assigned.",
    EventType.REVIEW_ACCEPTED: "A review of proposal {proposal_name} was accepted.",
    EventType.REVIEW_SUBMITTED: "A review of proposal {proposal_name} was submitted.",
    EventType.REVIEW_REJECTED: "A review of proposal {proposal_name} was rejected.",
    EventType.REVIEW_CANCELED: "A review of proposal {proposal_name} was canceled.",
}


def log_review_event(review, event_type, extra_scopes=()):
    """Log a review event on the review's feed and its proposal's."""
    _emit(
        REVIEW_MESSAGES[event_type],
        event_type,
        review.proposal,
        scopes=[review, review.proposal, *extra_scopes],
        review=review,
    )


def log_review_declined(assignment_item):
    """An assignment declined has no review: logged on the proposal's feed,
    where the call team and the one who declined read it."""
    _emit(
        "A review of proposal {proposal_name} was declined.",
        EventType.REVIEW_DECLINED,
        assignment_item.proposal,
    )


def log_change_request(change_request, trigger):
    """Log a request for changes being made or closed (submitted ones are
    logged together, ``log_changes_submitted``).

    A request to the applicant is logged where the applicant team reads it,
    with the parts it unlocks; the message itself is in the request. The
    reopening of a contribution is the call team's: the applicant team does
    not read it, nor its message. ``trigger`` None is a request called off
    or lapsed.
    """
    instance = change_request.step_instance
    proposal = change_request.proposal
    context = {
        "step_name": _step_name(instance),
        "change_request_uuid": change_request.uuid.hex,
    }
    if trigger == NotificationRuleTriggers.CONTRIBUTION_REOPENED:
        _emit(
            "A contribution to step {step_name} of proposal {proposal_name} "
            "was reopened.",
            EventType.PROPOSAL_CONTRIBUTION_REOPENED,
            proposal,
            **context,
        )
        return
    if trigger == NotificationRuleTriggers.CHANGES_REQUESTED:
        # Whoever acts on the step may ask, an evaluator included: the
        # applicant team is not told who.
        with without_actor():
            _emit(
                "Changes to proposal {proposal_name} were requested at step "
                "{step_name}.",
                EventType.PROPOSAL_CHANGES_REQUESTED,
                proposal,
                unlocked_parts=list(change_request.unlocked_parts),
                **context,
            )
        return
    with without_actor():
        _emit(
            "Request for changes to proposal {proposal_name} at step {step_name} "
            "was closed: {resolution}.",
            EventType.PROPOSAL_CHANGE_REQUEST_CLOSED,
            proposal,
            resolution=change_request.resolution,
            **context,
        )


def log_changes_submitted(proposal, closed):
    """Log one submission of the changes asked of the applicant, naming
    every request it closed (``closed``, earliest first): the steps that
    asked, and the parts each had unlocked."""
    step_names = [_step_name(change_request.step_instance) for change_request in closed]
    unlocked = []
    for change_request in closed:
        for part in change_request.unlocked_parts:
            if part not in unlocked:
                unlocked.append(part)
    _emit(
        "Changes to proposal {proposal_name} requested at step {step_name} "
        "were submitted."
        if len(closed) == 1
        else "Changes to proposal {proposal_name} requested at steps {step_name} "
        "were submitted.",
        EventType.PROPOSAL_CHANGES_SUBMITTED,
        proposal,
        step_name=", ".join(step_names),
        change_request_uuid=closed[0].uuid.hex,
        change_requests=[
            {
                "uuid": change_request.uuid.hex,
                "step_name": step_name,
                "unlocked_parts": list(change_request.unlocked_parts),
            }
            for change_request, step_name in zip(closed, step_names)
        ],
        unlocked_parts=unlocked,
    )


def log_notification_sent(
    proposal,
    template,
    trigger,
    audience,
    count,
    step_instance=None,
    email_log_uuids=(),
):
    """Log a workflow mail that went out: what told whom, never an address.

    ``audience`` is the role the mail addressed (a notification rule's
    recipient, or the audience of a mail the workflow always sends) and
    ``count`` how many recipients it reached. Logged as no one's and only
    for the call team: the applicant team does not read it, and an evaluator
    reads only their own actions.
    """
    if not count:
        return
    context = {
        "template": template,
        "trigger": trigger or "",
        "audience": audience or "",
        "recipient_count": count,
        # The email log rows the mail wrote; its content and addresses stay
        # there, read through the proposal's workflow-mails.
        "email_log_uuids": list(email_log_uuids),
    }
    if step_instance is not None:
        context.update(
            step_name=_step_name(step_instance),
            step_key=step_instance.call_step.key,
            step_instance_uuid=step_instance.uuid.hex,
        )
    with without_actor():
        _emit(
            "Workflow mail {template} on proposal {proposal_name} was sent to "
            "{recipient_count} recipient(s) ({audience}).",
            EventType.PROPOSAL_WORKFLOW_NOTIFICATION_SENT,
            proposal,
            **context,
        )
