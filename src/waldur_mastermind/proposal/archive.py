"""An archived call is read-only: what that freezes, in one place.

Archiving a call closes its evaluation: its configuration, its proposals and
the work on them up to the award, and the periodic tasks that move them on,
stand where they are until the call is activated again. The award stage goes
on: the award response, provisioning the award, extensions and the final
report, and the steps and contributions they run. Reads stay open.

The guard is applied at three levels, all of them reading this module:

- the views: ``permissions.ArchivedCallIsReadOnly`` refuses every write to an
  object of an archived call (409, ``CALL_ARCHIVED_MESSAGE``);
- the services that complete steps (``refuse_step`` and ``freezes_step``), so
  a path that is not a view -- a role revoked, a conflict settled -- cannot
  move a proposal on either;
- the periodic tasks, which leave the objects ``frozen_q`` matches alone.

The award stage is defined here once: ``POST_AWARD_ACTIONS`` for the view
actions that are only that, ``is_post_award_step`` / ``post_award_step_q``
for steps (the post-award phases and the award response). Editing the award
is not part of it: that is the allocation decision's, in the evaluation.
"""

from django.db.models import Q

from waldur_core.core.exceptions import IncorrectStateException
from waldur_mastermind.proposal import dry_run_context, models
from waldur_mastermind.proposal.enums import (
    AWARD_RESPONSE_KEY,
    CallStates,
    WorkflowPhases,
)

# The view actions that are award-stage work, which an archived call lets
# through: on an accepted proposal, the provisioning of its award, and its
# runs.
POST_AWARD_ACTIONS = frozenset(
    {
        "provision_awarded_item",
        "request_extension",
        "open_final_report",
        "withdraw_run",
        "run_detail",
    }
)

# The view actions on a proposal's steps -- their decision, contributions,
# forms, the changes they ask for, their reports -- which act on award-stage
# steps as well as on the evaluation's. The view lets them through; each
# action refuses once it knows the step it acts on (``refuse_step``).
STEP_WORK_ACTIONS = frozenset(
    {
        "complete_workflow_step",
        "reject_workflow_step",
        "advance_workflow_step",
        "submit_contribution",
        "reopen_contribution",
        "decline_contribution",
        "delegate_contribution",
        "accept_contribution",
        "submit_step_checklist_answers",
        "request_changes",
        "submit_changes",
        "cancel_change_request",
        "release_step_report",
    }
)

# How an object reaches its call, by the first foreign key it has: the field
# holding the id, and the lookup from Call to an object holding it.
_CALL_PATHS = (
    ("call_id", "pk"),
    ("round_id", "round"),
    ("proposal_id", "round__proposal"),
    ("batch_id", "assignment_batches"),
    ("proposal_step_id", "round__proposal__workflow_step_instances"),
    ("round_step_id", "round__steps"),
)


def is_archived(call) -> bool:
    return call is not None and call.state == CallStates.ARCHIVED


def _cached_call_id(obj):
    """The id of the call loaded on ``obj`` or along its path, if any."""
    if isinstance(obj, models.Call):
        return obj.pk
    cache = getattr(getattr(obj, "_state", None), "fields_cache", {})
    for name in ("call", "round", "proposal", "batch"):
        related = cache.get(name)
        if related is not None:
            return _cached_call_id(related)
    return None


def call_state_of(obj) -> str | None:
    """The state of the call ``obj`` belongs to: a call, or anything of one
    (a round, a proposal, a review, an assignment...). One query, always
    fresh: an object loaded earlier may carry a call since archived."""
    call_id = _cached_call_id(obj)
    if call_id is not None:
        lookup = {"pk": call_id}
    else:
        lookup = None
        for field, path in _CALL_PATHS:
            value = getattr(obj, field, None)
            if value is not None:
                lookup = {path: value}
                break
        if lookup is None:
            return None
    return models.Call.objects.filter(**lookup).values_list("state", flat=True).first()


def is_archived_object(obj) -> bool:
    return call_state_of(obj) == CallStates.ARCHIVED


def refuse(obj):
    """Refuse a write to ``obj`` while its call is archived (409)."""
    if is_archived_object(obj):
        raise IncorrectStateException(models.CALL_ARCHIVED_MESSAGE)


def refuses_action(obj, action) -> bool:
    """Whether an archived call refuses the view action ``action`` on
    ``obj`` up front: everything but the award stage. Step work is left to
    the action, which checks the very step it acts on (``refuse_step``)."""
    if action in POST_AWARD_ACTIONS or not is_archived_object(obj):
        return False
    return not (action in STEP_WORK_ACTIONS and isinstance(obj, models.Proposal))


def is_award_stage_call_step(call_step) -> bool:
    """Whether the call step is award-stage work: a post-award phase's, or
    the award response."""
    return (
        call_step.phase in WorkflowPhases.POST_AWARD
        or call_step.key == AWARD_RESPONSE_KEY
    )


def is_post_award_step(instance) -> bool:
    return is_award_stage_call_step(instance.call_step)


def freezes_step(proposal, instance=None) -> bool:
    """Whether the proposal's call being archived stands ``instance`` (or
    the proposal's work, without one) still. Not in a dry run: it plays
    the workflow on a copy, whatever the call's state."""
    if dry_run_context.current() is not None or not is_archived_object(proposal):
        return False
    return instance is None or not is_post_award_step(instance)


def freezes_stage(proposal, members) -> bool:
    """Whether an archived call keeps the proposal from entering the stage
    of ``members``: one that is not all award-stage work."""
    if dry_run_context.current() is not None or not is_archived_object(proposal):
        return False
    return not all(is_post_award_step(member) for member in members)


def refuse_step(proposal, instance=None):
    """Refuse completing, rejecting or advancing a step of a proposal of an
    archived call (409)."""
    if freezes_step(proposal, instance):
        raise IncorrectStateException(models.CALL_ARCHIVED_MESSAGE)


def archived_q(call_path: str) -> Q:
    """Objects of an archived call, ``call_path`` leading to the call."""
    return Q(**{f"{call_path}__state": CallStates.ARCHIVED})


def post_award_step_q(call_step_path: str = "call_step") -> Q:
    """Step instances (or anything, by ``call_step_path``) of post-award work."""
    return Q(**{f"{call_step_path}__phase__in": WorkflowPhases.POST_AWARD}) | Q(
        **{f"{call_step_path}__key": AWARD_RESPONSE_KEY}
    )


def frozen_steps_q(prefix: str = "") -> Q:
    """Step instances an archived call stands still -- those of its
    evaluation -- or, with ``prefix`` (``"step_instance__"``), what hangs
    off them."""
    return archived_q(f"{prefix}proposal__round__call") & ~post_award_step_q(
        f"{prefix}call_step"
    )
