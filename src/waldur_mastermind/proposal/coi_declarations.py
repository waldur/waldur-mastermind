"""The conflict-of-interest declaration an evaluator confirms.

A step asking its evaluators to confirm having no conflict of interest
(``requires_coi_confirmation``) can word the declaration itself
(``CallWorkflowStep.coi_declaration_text``); otherwise the call's wording
applies (``Call.coi_declaration_text``), and without that the platform's
standard declaration, which the client words and an empty text stands for.

Each confirmation keeps the text confirmed, as worded when it was: a later
edit of the step or the call never changes what someone declared.
"""

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import ContributorSources, StepKinds

# Where a declaration's wording comes from.
STEP = "step"
CALL = "call"
PLATFORM = "platform"


def resolve(step_text, call_text) -> tuple[str, str]:
    """``(text, source)`` of a step's declaration from the step's and the
    call's wording; an empty text is the platform's standard declaration."""
    step_text = (step_text or "").strip()
    if step_text:
        return step_text, STEP
    call_text = (call_text or "").strip()
    if call_text:
        return call_text, CALL
    return "", PLATFORM


def for_step(call_step) -> str:
    """The declaration ``call_step``'s evaluators confirm."""
    if call_step is None:
        return ""
    return resolve(call_step.coi_declaration_text, call_step.call.coi_declaration_text)[
        0
    ]


def _call_default(call) -> str:
    return resolve("", call.coi_declaration_text)[0]


def _first_step(call, **filters):
    return (
        models.CallWorkflowStep.objects.filter(
            call=call, is_enabled=True, requires_coi_confirmation=True, **filters
        )
        .select_related("call")
        .order_by("order", "id")
        .first()
    )


def for_assignment(assignment) -> str:
    """The declaration the holder of an evaluator role confirms: that of the
    step they were assigned in; for a holder assigned outside a step, that
    of the call's first assignment step for the role asking for one."""
    if assignment.assigned_in_id is not None:
        return for_step(assignment.assigned_in.call_step)
    call = assignment.role.call
    step = _first_step(call, kind=StepKinds.ASSIGNMENT, assigns_roles=assignment.role)
    return for_step(step) if step is not None else _call_default(call)


def for_review(review) -> str:
    """The declaration a reviewer confirms with their review: that of the
    step it contributes to; for a review outside a step, that of the call's
    first step its reviewers contribute to asking for one."""
    contribution = getattr(review, "contribution", None)
    if contribution is not None:
        call_step = contribution.proposal_step.call_step
        if call_step.requires_coi_confirmation:
            return for_step(call_step)
    call = review.proposal.round.call
    step = _first_step(call, contributor_source=ContributorSources.ASSIGNED_REVIEWERS)
    if step is None:
        step = _first_step(call)
    return for_step(step) if step is not None else _call_default(call)


def record_contribution_confirmation(contribution, user):
    """Keep the confirmation ``user`` gives on taking ``contribution`` over:
    who, when, the wording and where it came from. Nothing when its step
    asks for none."""
    call_step = contribution.proposal_step.call_step
    if not call_step.requires_coi_confirmation:
        return None
    text, source = resolve(
        call_step.coi_declaration_text, call_step.call.coi_declaration_text
    )
    return models.StepContributionCoiConfirmation.objects.create(
        contribution=contribution,
        user=user,
        declaration_text=text,
        declaration_source=source,
    )


def for_contribution(contribution) -> str:
    """The declaration taking ``contribution`` over asks of its delegate;
    empty when its step asks for none."""
    call_step = contribution.proposal_step.call_step
    if not call_step.requires_coi_confirmation:
        return ""
    return for_step(call_step)
