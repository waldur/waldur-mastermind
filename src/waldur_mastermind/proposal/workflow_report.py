"""A call's workflow validation as a PDF.

What the call team reads on the call's Validation panel -- the steps, who
sees, contributes to, completes and is told about each, the workflow check's
findings and the last scenario run -- on paper. It is
built from the same functions as the panel's endpoints, so it says nothing
those do not: rules and counts, never people.

Rendered with Typst like the invoice PDF: every value reaches the template as
a string already formatted and translated here, and is inserted as text.
"""

import json
import threading
from pathlib import Path
from typing import cast

import typst
from django.utils import timezone
from django.utils.translation import gettext as _

from waldur_mastermind.invoices.utils import INVOICE_PDF_DIR
from waldur_mastermind.proposal import (
    workflow_checks,
    workflow_description,
    workflow_scenarios,
    workflow_versions,
)
from waldur_mastermind.proposal.enums import (
    ContributorSources,
    NotificationRuleTriggers,
    ResponsibleRoles,
    StepKinds,
    TransitionModes,
    WorkflowPhases,
    WorkflowStepOutcomes,
)

REPORT_PDF_DIR = Path(__file__).parent / "pdf"

_LABELS = {
    "kind": dict(StepKinds.CHOICES),
    "phase": dict(WorkflowPhases.CHOICES),
    "completer": dict(ResponsibleRoles.CHOICES),
    "contributors": dict(ContributorSources.CHOICES),
    "trigger": dict(NotificationRuleTriggers.CHOICES),
    "outcome": dict(WorkflowStepOutcomes.CHOICES),
}

# The settings a finding names, in the words of the pages that edit them.
FIELD_LABELS = {
    "rounds": "Rounds",
    "offerings": "Offerings",
    "workflow_steps": "Steps",
    "proposal_roles": "Evaluator roles",
    "panels": "Panels",
    "panel_chair": "Panel chair",
    "budgets": "Resource pools",
    "budget_enforcement": "Budget enforcement",
    "reviewer_pool": "Reviewer pool",
    "target_decision_days": "Target decision time",
    "evaluation_start": "Evaluation start",
    "reviewer_identity_visible_to_submitters": "Reviewer identity",
    "kind": "Kind",
    "completer": "Completed by",
    "completer_proposal_role": "Completing evaluator role",
    "contributor_source": "Contributors",
    "contributors_lead_only": "Only the lead contributes",
    "per_requested_offering": "One contribution per requested offering",
    "delegable_to": "Can be handed to",
    "input_steps": "Reads the outputs of",
    "applicant_visible": "Visible to the applicant",
    "release_to_applicant": "Report released to the applicant",
    "blind_review": "Blind review",
    "is_allocation_decision": "Allocation decision",
    "decides_automatically": "Decides automatically",
    "duration_in_days": "Duration",
    "notification_rules": "Notification rules",
    "run_condition": "Run condition",
    "status_labels": "Status labels",
    "transition_mode": "Transition",
    "outcome_set": "Outcomes",
    "assigns_roles": "Assigns roles",
    "checklist": "Checklist",
    "min_score_threshold": "Minimum score",
}

SEVERITY_LABELS = {"error": "Error", "warning": "Warning", "info": "Note"}
SCENARIO_LABELS = {"passed": "Passed", "failed": "Failed", "error": "Error"}

# The role matrix's marks, one letter each, in the order cells show them.
ROLE_FLAGS = (
    ("sees", "S", "Sees the step"),
    ("reads_outputs", "R", "Reads its outputs"),
    ("contributes", "C", "Contributes"),
    ("completes", "D", "Completes (decides)"),
    ("notified", "N", "Is notified"),
)


def _label(kind, value) -> str:
    if value in (None, ""):
        return ""
    label = _LABELS[kind].get(value)
    return _(label) if label else str(value).replace("_", " ").capitalize()


def field_label(field) -> str:
    if not field:
        return ""
    label = FIELD_LABELS.get(field)
    return _(label) if label else field.replace("_", " ").capitalize()


def _actor_titles(description) -> dict:
    return {actor["key"]: actor["label"] for actor in description["actors"]}


def _contributors(step, actors) -> str:
    contributors = step["contributors"]
    source = contributors["source"]
    if source in (None, "", ContributorSources.NONE):
        return ""
    if source == ContributorSources.CALL_ROLE and contributors["parameter"]:
        text = _label("completer", contributors["parameter"])
    elif source == ContributorSources.PROPOSAL_ROLE and contributors["parameter"]:
        text = actors.get(contributors["parameter"]) or _label("contributors", source)
    else:
        text = _label("contributors", source)
    if contributors.get("min"):
        text += " " + _("(at least {count})").format(count=contributors["min"])
    if contributors.get("per_requested_offering"):
        text += ", " + _("one per requested offering")
    return text


def _step_row(step, actors) -> dict:
    completer = _label("completer", step["completer"])
    if step["completer"] == ResponsibleRoles.PROPOSAL_ROLE:
        completer = actors.get(step["completer_proposal_role"] or "") or completer
    if step["decides_automatically"]:
        completer = _("Decided automatically")
    notes = []
    if not step["enabled"]:
        notes.append(_("Disabled"))
    if step["parallel_with_previous"]:
        notes.append(_("Runs alongside the step before"))
    if step["is_allocation_decision"]:
        notes.append(_("Allocation decision"))
    if step["transition_mode"] == TransitionModes.MANUAL:
        notes.append(_("Held for a manual advance"))
    if step["applicant"]["visible"]:
        notes.append(_("Visible to the applicant"))
    if step["blind_review"]:
        notes.append(_("Blind review"))
    return {
        "number": str(step["order"] + 1),
        "name": step["name"],
        "enabled": bool(step["enabled"]),
        "phase": _label("phase", step["phase"]),
        "kind": _label("kind", step["kind"]),
        "description": step["description"] or "",
        "completer": completer,
        "contributors": _contributors(step, actors),
        "duration": (
            _("{days} days").format(days=step["duration_in_days"])
            if step["duration_in_days"]
            else ""
        ),
        "outcomes": ", ".join(
            _label("outcome", outcome["value"]) for outcome in step["outcomes"]
        ),
        "notes": ", ".join(notes),
    }


def _trigger_text(trigger) -> str:
    text = _label("trigger", trigger["trigger"])
    if trigger.get("days_before") is not None:
        text += " " + _("({days} d before)").format(days=trigger["days_before"])
    return text


def _matrices(description, actors) -> tuple[dict, dict]:
    keys = [actor["key"] for actor in description["actors"]]
    roles, notifications = [], []
    for step in description["steps"]:
        by_actor = {cell["actor"]: cell for cell in step["roles"]}
        role_cells, notification_cells = [], []
        for key in keys:
            cell = by_actor.get(key)
            if cell is None:
                role_cells.append("")
                notification_cells.append([])
                continue
            role_cells.append(
                " ".join(
                    letter for flag, letter, _text in ROLE_FLAGS if cell[flag]["value"]
                )
            )
            notification_cells.append(
                [_trigger_text(t) for t in cell["notified"].get("triggers", [])]
            )
        roles.append(
            {"step": step["name"], "enabled": step["enabled"], "cells": role_cells}
        )
        notifications.append(
            {
                "step": step["name"],
                "enabled": step["enabled"],
                "cells": notification_cells,
            }
        )
    columns = [actors[key] for key in keys]
    return (
        {
            "columns": columns,
            "rows": roles,
            "legend": [f"{letter} = {_(text)}" for _flag, letter, text in ROLE_FLAGS],
        },
        {"columns": columns, "rows": notifications},
    )


def _step_names(description) -> dict:
    return {step["key"]: step["name"] for step in description["steps"]}


def _finding_trigger(finding) -> str:
    """The event of the notification rule a finding is about, if it is."""
    for ref in finding.get("because") or []:
        if ref.get("source") == "rule" and ref.get("field") == "trigger":
            return _label("trigger", ref.get("detail"))
    return ""


def _finding_where(finding) -> str:
    field = field_label(finding.get("field"))
    trigger = _finding_trigger(finding)
    if trigger:
        field = _("{setting}, on {event}").format(setting=field, event=trigger.lower())
    if finding.get("step"):
        name = finding.get("step_name") or finding["step"]
        return f"{name}: {field}" if field else name
    return _("Call: {setting}").format(setting=field) if field else _("Call")


def get_workflow_report_data(call) -> dict:
    description = workflow_description.describe_call(call)
    actors = _actor_titles(description)
    names = _step_names(description)
    findings = workflow_checks.check_call(call)
    summary = workflow_checks.summary(findings)
    role_matrix, notification_matrix = _matrices(description, actors)

    flags = [
        {
            "severity": SEVERITY_LABELS.get(flag["severity"], flag["severity"]),
            "where": names.get(flag.get("step"), _("Call")),
            "message": flag["message"],
        }
        for flag in description["notifications"]["flags"]
        if flag["severity"] != "info"
    ]
    always_sent = [
        {
            "event": item["event"].replace("_", " ").capitalize(),
            "audience": ", ".join(actors.get(a, a) for a in item["audience"]),
        }
        for item in description["notifications"]["always_sent"]
    ]

    version, checks = workflow_scenarios.stored_scenario_checks(call)
    scenarios = []
    for result in checks.get("scenarios") or []:
        failure = result.get("failure") or {}
        scenarios.append(
            {
                "name": result.get("name") or result.get("key") or "",
                "status": SCENARIO_LABELS.get(result["status"], result["status"]),
                "passed": result["status"] == "passed",
                "detail": result.get("error") or failure.get("message") or "",
            }
        )

    latest = workflow_versions.owner_versions(call).order_by("-number").first()
    generated = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M")
    facts = [
        {"label": _("State"), "value": call.get_state_display()},
        {"label": _("Generated"), "value": generated},
    ]
    if latest is not None:
        facts.append({"label": _("Workflow version"), "value": str(latest.number)})
    if call.template_id:
        facts.append({"label": _("Template"), "value": call.template.name})

    if workflow_checks.errors(findings):
        verdict = _("The call cannot be activated until the errors are fixed.")
    else:
        verdict = _("Nothing in the workflow stops the call from being activated.")

    return {
        "title": _("Workflow validation"),
        "call": call.name,
        "facts": facts,
        "verdict": verdict,
        "labels": {
            "check": _("Workflow check"),
            "steps": _("Steps"),
            "roles": _("Roles"),
            "notifications": _("Notifications"),
            "always_sent": _("Sent on every call"),
            "flags": _("Notification warnings"),
            "scenarios": _("Scenarios"),
            "finding": _("Finding"),
            "where": _("Where"),
            "severity": _("Severity"),
            "step": _("Step"),
            "kind": _("Kind"),
            "completer": _("Completed by"),
            "contributors": _("Contributors"),
            "duration": _("Duration"),
            "outcomes": _("Outcomes"),
            "event": _("Event"),
            "audience": _("Who is told"),
            "status": _("Status"),
            "scenario": _("Scenario"),
            "detail": _("Detail"),
            "no_findings": _("No errors, warnings or notes."),
            "no_scenarios": _("The call's scenarios have not been run."),
            "roles_intro": _("Who takes part in each step."),
            "notifications_intro": _(
                "Who each step's notification rules tell, and on which event."
            ),
        },
        "summary": {
            "error": str(summary["error"]),
            "warning": str(summary["warning"]),
            "info": str(summary["info"]),
            "error_label": _("Errors"),
            "warning_label": _("Warnings"),
            "info_label": _("Notes"),
        },
        "findings": [
            {
                "severity": SEVERITY_LABELS[f["severity"]],
                "level": f["severity"],
                "where": _finding_where(f),
                "message": (
                    f"{f['step_name']}: {f['message']}"
                    if f.get("step_name")
                    else f["message"]
                ),
            }
            for f in findings
        ],
        "steps": [_step_row(step, actors) for step in description["steps"]],
        "role_matrix": role_matrix,
        "notification_matrix": notification_matrix,
        "flags": flags,
        "always_sent": always_sent,
        "scenarios": scenarios,
        "scenarios_checked": (
            _("Last run on version {number}, {at}.").format(
                number=version.number, at=(checks.get("checked_at") or "")[:16]
            )
            if version is not None
            else ""
        ),
    }


_compiler = None
_compiler_lock = threading.Lock()


def create_workflow_report_pdf(call) -> bytes:
    """Render the report. The compiler is built once per process, as the
    invoice's is; the template imports nothing and reads only its inputs."""
    global _compiler
    data = json.dumps(get_workflow_report_data(call))
    with _compiler_lock:
        if _compiler is None:
            _compiler = typst.Compiler(
                str(REPORT_PDF_DIR / "workflow_report.typ"),
                root=str(REPORT_PDF_DIR),
                font_paths=[str(INVOICE_PDF_DIR / "fonts")],
            )
        # One document in, one PDF out: never a list of pages, never None.
        return cast(bytes, _compiler.compile(sys_inputs={"report": data}))
