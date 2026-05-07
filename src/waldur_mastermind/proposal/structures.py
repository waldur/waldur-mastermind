from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WorkflowStepDefinition:
    """Definition of a workflow step in the proposal evaluation process.

    These are the predefined step types available for call configuration.
    Admins enable/disable steps per call and configure roles, durations,
    and evaluation checklists for each enabled step.
    """

    id: str
    name: str
    description: str
    is_mandatory: bool = False
    dependencies: list[str] = field(default_factory=list)
    default_responsible_role: str | None = None
