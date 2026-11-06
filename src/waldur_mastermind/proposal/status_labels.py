"""What each audience's dashboard calls a proposal's workflow step.

A call step may say, per audience, what the step is called while it runs,
once it has ended, per outcome and while it has sent the proposal back to the
applicant for changes (``CallWorkflowStep.status_labels``). Whatever it
leaves out falls back to the label the reader's view computes today: the
step's name and status, or the applicant tracker's stand-in for a step kept
from the applicant.

The labels are the call's own configuration text, shown as entered, as the
step's name is.

Who reads which label:

- the applicant team reads the applicant labels of steps visible to the
  applicant, of the allocation decision and of the decision closing an
  extension request or final report, whose outcomes are theirs anyway
  (``applies_to_applicant``): any other step kept from them keeps its
  stand-in ("Internal check"), whatever the call configured for it;
- the call team (its managers, staff and support: the admin portal) reads
  the call team labels;
- the offering teams (the hosting entities' portal) read the offering team
  labels of the steps they work on;
- every other evaluator reads the evaluator labels.

The call team's and the offering teams' labels fall back, state by state, to
the evaluators': a call that names its steps one way for everyone behind the
applicant configures the evaluators alone. Who reads as which audience is
decided in one place, ``ProposalWorkflowStepInstanceSerializer.label_audience``.

The label is picked from what the reader is shown of the step: an outcome
withheld from them never picks its outcome label.

A label may show counts of the step's work, as placeholders filled per
proposal (``PLACEHOLDERS``): "{submitted} of {expected} reviews submitted".
Counts never name anyone, and the applicant's labels take none: the applicant
is not told how their evaluation is staffed.
"""

import re

from django.core.exceptions import ValidationError as DjangoValidationError

from waldur_mastermind.proposal.enums import (
    OutcomeSets,
    StepKinds,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
)

APPLICANT = "applicant"
EVALUATORS = "evaluators"
CALL_TEAM = "call_team"
OFFERING_TEAM = "offering_team"
AUDIENCES = (APPLICANT, EVALUATORS, CALL_TEAM, OFFERING_TEAM)
# The audience whose labels stand in, state by state, for those left unset.
FALLBACK = {CALL_TEAM: EVALUATORS, OFFERING_TEAM: EVALUATORS}

ACTIVE = "active"
COMPLETED = "completed"
CHANGES_REQUESTED = "changes_requested"
OUTCOMES = "outcomes"
STATES = (ACTIVE, COMPLETED, CHANGES_REQUESTED)

MAX_LENGTH = 255

# Counts a label can show, filled per proposal from the step's work
# (``contributions.step_counts``).
INVITED = "invited"
ACCEPTED = "accepted"
DECLINED = "declined"
SUBMITTED = "submitted"
EXPECTED = "expected"
REPORTS_SUBMITTED = "reports_submitted"
REPORTS_EXPECTED = "reports_expected"
PLACEHOLDERS = {
    INVITED: "people asked to contribute: invited, or given a slot",
    ACCEPTED: "contributions taken on or submitted",
    DECLINED: "invitations and contributions declined",
    SUBMITTED: "contributions submitted that count towards the step",
    EXPECTED: "contributions the step needs: its minimum, else its open and "
    "submitted ones",
    REPORTS_SUBMITTED: "the same as submitted, for a step of reports",
    REPORTS_EXPECTED: "the same as expected, for a step of reports",
}
# Anything in braces is a placeholder, and must be one of PLACEHOLDERS.
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


def _invalid(message):
    return DjangoValidationError({"status_labels": message})


def placeholders(text) -> list[str]:
    """The placeholders ``text`` names, in order."""
    return PLACEHOLDER.findall(text) if isinstance(text, str) else []


def _text(value, where, audience):
    if not isinstance(value, str):
        raise _invalid(f"{where} is a text.")
    if len(value) > MAX_LENGTH:
        raise _invalid(f"{where} is longer than {MAX_LENGTH} characters.")
    named = placeholders(value)
    unknown = sorted(set(named) - set(PLACEHOLDERS))
    if unknown:
        raise _invalid(
            f"{where} names unknown placeholder(s): "
            f"{', '.join('{' + n + '}' for n in unknown)}. "
            f"Allowed: {', '.join('{' + n + '}' for n in PLACEHOLDERS)}."
        )
    if named and audience == APPLICANT:
        raise _invalid(
            f"{where} cannot show counts: the applicant is not told how "
            "their evaluation is staffed."
        )
    return value.strip()


def clean(labels, outcome_set):
    """``labels`` normalised: texts stripped, empty texts and audiences
    dropped. Refuses audiences, states and outcomes a step does not have."""
    if labels in (None, ""):
        return {}
    if not isinstance(labels, dict):
        raise _invalid(
            'Status labels are {"applicant": {...}, "evaluators": {...}, '
            '"call_team": {...}, "offering_team": {...}}, or empty to keep '
            "the computed labels."
        )
    unknown = set(labels) - set(AUDIENCES)
    if unknown:
        raise _invalid(
            f"Unknown audience(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(AUDIENCES)}."
        )
    allowed_outcomes = OutcomeSets.OUTCOMES.get(outcome_set, ())
    cleaned = {}
    for audience in AUDIENCES:
        entry = labels.get(audience)
        if entry in (None, ""):
            continue
        if not isinstance(entry, dict):
            raise _invalid(f"The {audience} labels are an object.")
        unknown = set(entry) - set(STATES) - {OUTCOMES}
        if unknown:
            raise _invalid(
                f"Unknown {audience} label(s): {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join((*STATES, OUTCOMES))}."
            )
        result = {}
        for state in STATES:
            if entry.get(state) is not None:
                text = _text(entry[state], f"The {audience} '{state}' label", audience)
                if text:
                    result[state] = text
        outcomes = entry.get(OUTCOMES)
        if outcomes not in (None, ""):
            if not isinstance(outcomes, dict):
                raise _invalid(f"The {audience} outcome labels are an object.")
            unknown = set(outcomes) - set(allowed_outcomes)
            if unknown:
                raise _invalid(
                    f"The step cannot end with {', '.join(sorted(unknown))}. "
                    f"Its outcomes: {', '.join(allowed_outcomes) or 'none'}."
                )
            texts = {}
            for outcome in allowed_outcomes:
                if outcomes.get(outcome) is not None:
                    text = _text(
                        outcomes[outcome],
                        f"The {audience} label for '{outcome}'",
                        audience,
                    )
                    if text:
                        texts[outcome] = text
            if texts:
                result[OUTCOMES] = texts
        if result:
            cleaned[audience] = result
    return cleaned


def applies_to_applicant(step) -> bool:
    """Whether the applicant labels of ``step`` -- a call step, or its
    document (a dict) -- reach the applicant team: on a step visible to them,
    on the allocation decision, and on the decision closing an extension
    request or final report, whose outcomes the applicant learns whether or
    not the step is shown."""
    if isinstance(step, dict):
        values = step
    else:
        values = {
            "applicant_visible": step.applicant_visible,
            "is_allocation_decision": step.is_allocation_decision,
            "phase": step.phase,
            "kind": step.kind,
        }
    return bool(
        values.get("applicant_visible")
        or values.get("is_allocation_decision")
        or (
            values.get("phase") in WorkflowPhases.POST_AWARD
            and values.get("kind") == StepKinds.DECISION
        )
    )


def _entry(labels, audience):
    entry = labels.get(audience) if isinstance(labels, dict) else None
    return entry if isinstance(entry, dict) else {}


def configured(call_step, audience):
    """The labels ``call_step`` -- a call step or its document -- gives
    ``audience``: the applicant's only where they reach the applicant; the
    call team's and the offering teams' completed, state by state, by the
    evaluators'."""
    if call_step is None:
        return {}
    if audience == APPLICANT and not applies_to_applicant(call_step):
        return {}
    if isinstance(call_step, dict):
        labels = call_step.get("status_labels") or {}
    else:
        labels = call_step.status_labels or {}
    entry = _entry(labels, audience)
    fallback = FALLBACK.get(audience)
    if not fallback:
        return entry
    base = _entry(labels, fallback)
    if not base:
        return entry
    merged = {**base, **entry}
    outcomes = {**(base.get(OUTCOMES) or {}), **(entry.get(OUTCOMES) or {})}
    if outcomes:
        merged[OUTCOMES] = outcomes
    return merged


def render(text, counts):
    """``text`` with its placeholders filled from ``counts`` -- a mapping,
    or a callable returning one, called only when the text has any."""
    if not text or not placeholders(text):
        return text
    values = counts() if callable(counts) else counts
    values = values or {}
    return PLACEHOLDER.sub(
        lambda match: str(values.get(match.group(1), match.group(0))), text
    )


def pick(entry, status, outcome=None, changes_requested=False):
    """The label of ``entry`` (one audience's labels) for a step in
    ``status`` with ``outcome`` -- what the reader is shown of it -- or None
    to fall back to the computed label."""
    if not entry:
        return None
    if status == WorkflowStepInstanceStatuses.ACTIVE:
        if changes_requested and entry.get(CHANGES_REQUESTED):
            return entry[CHANGES_REQUESTED]
        return entry.get(ACTIVE) or None
    if status == WorkflowStepInstanceStatuses.COMPLETED:
        if outcome:
            text = (entry.get(OUTCOMES) or {}).get(outcome)
            if text:
                return text
        return entry.get(COMPLETED) or None
    return None
