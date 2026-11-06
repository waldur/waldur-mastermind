from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StepKindDefinition:
    """Behaviour shared by every call step of one kind.

    Kinds are defined in code; a call manager composes a call's workflow from
    ordered, named call steps, each of one kind. A kind may appear any number
    of times in one call.
    """

    id: str
    name: str
    description: str
    # Outcome sets a step of this kind may use; the first is the default.
    outcome_sets: tuple[str, ...]
    default_completer: str
    # Call step fields that configure a step of this kind, beyond the ones
    # every step has (see COMMON_STEP_FIELDS).
    config_fields: tuple[str, ...] = ()
    # (trigger, recipient, days_before) rules seeded on a new step of this
    # kind, on top of the rules every step gets.
    default_notification_rules: tuple[tuple[str, str, int | None], ...] = ()
    # Whether the applicant is told about the step's progress at all.
    applicant_facing: bool = False

    @property
    def default_outcome_set(self) -> str:
        return self.outcome_sets[0]


@dataclass(frozen=True)
class DefaultCallStep:
    """A call step a new call starts with.

    The first five make up the workflow seeded on every new call; the award
    response is added when the allocation decision asks for it. They are also
    the fixed catalogue of steps earlier versions offered, so these keys are
    how an older export, preset or API client names a step.
    """

    key: str
    kind: str
    name: str
    description: str
    outcome_set: str
    completer: str
    on_negative: str
    is_allocation_decision: bool = False
    contributor_source: str = "none"
    extra: dict = field(default_factory=dict)
