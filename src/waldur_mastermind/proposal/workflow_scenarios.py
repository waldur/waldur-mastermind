"""Saved scenarios of a call template.

A template carries, next to its document, ``scenarios`` written by staff:
named paths through the workflow with what must hold at their end:
``{key, name, description, request, assertions[]}``. A scenario is a dry run
(``dry_run``) of ``request``'s actions, so it exercises the real engine and
sends nothing.

On the template they are definitions only. They run on each call created
from the template, on demand from the call's Validation page, as dry runs of
the call's own workflow, with the results kept on the call's latest version
(``WorkflowVersion.checks``).
"""

import logging
import time
from collections.abc import Iterable
from datetime import datetime, timedelta

from celery.exceptions import SoftTimeLimitExceeded
from django.db import transaction
from django.utils import timezone

from waldur_mastermind.proposal import (
    dry_run,
    models,
    status_labels,
    tasks,
    tripwires,
    workflow_description,
    workflow_observability,
    workflow_versions,
)
from waldur_mastermind.proposal.enums import (
    CheckRunStatuses,
    ProposalStates,
    RequestedOfferingStates,
    WorkflowStepInstanceStatuses,
)

logger = logging.getLogger(__name__)

CHECKS_SCHEMA = 1
MAX_SCENARIOS = 20
MAX_ASSERTIONS = 60
MAX_SCENARIO_OFFERINGS = 10

PASSED = "passed"
FAILED = "failed"
ERROR = "error"

# Whether a call's scenarios asked for on demand are still running.
RUNNING = CheckRunStatuses.RUNNING
IDLE = CheckRunStatuses.IDLE
# A run asked for this long ago that left no results is taken as lost, and
# may be asked for again.
CHECK_RUN_TIMEOUT = timedelta(minutes=15)
# A run in the background stops after this long, well before it is taken as
# lost; what it had not reached by then is reported as an error.
CHECK_RUN_SOFT_LIMIT_SECONDS = 600

BUSY = "Another dry run of the call is in progress."
BUSY_NOT_KEPT = (
    "Another dry run of the call was in progress while the scenarios ran: "
    "the earlier results are kept. Run the scenarios again."
)
TOO_LONG_NOT_KEPT = "The scenarios took too long and were stopped."
# Where a call's version records that its scenarios were asked to run, and
# what kept the last run's results from being kept.
SCENARIOS_REQUESTED_AT = "scenarios_requested_at"
SCENARIOS_ERROR = "scenarios_error"

# The kinds of a scenario's assertion: each entry names exactly one.
ASSERTION_KINDS = (
    "proposal_state",
    "step_status",
    "notified",
    "not_notified",
    "label",
    "visible",
    "hidden",
    "ends_at",
)

STEP_RESULT_STATUSES = (
    WorkflowStepInstanceStatuses.ACTIVE,
    WorkflowStepInstanceStatuses.SKIPPED,
    WorkflowStepInstanceStatuses.EXPIRED,
    WorkflowStepInstanceStatuses.WITHDRAWN,
)


# =============================================================================
# Scenarios
# =============================================================================


def same_text(actual, expected):
    """Whether two dashboard labels read the same: their case and spacing
    are presentation."""
    if actual is None or expected is None:
        return actual == expected
    return " ".join(actual.split()).casefold() == " ".join(expected.split()).casefold()


def _latest(result, key):
    """The latest instance of step ``key`` in a dry run's result: a step
    after the award runs again in each run."""
    found = None
    for step in result["steps"]:
        if step["key"] == key:
            found = step
    return found


def path_of(result) -> list[list]:
    """The (step, result) pairs of a dry run's proposal, as
    ``workflow_observability.paths`` reads a real proposal's: steps not yet
    reached left out, a completed step by its outcome."""
    path = []
    for step in result["steps"]:
        status = step["status"]
        if status == WorkflowStepInstanceStatuses.PENDING:
            continue
        outcome = step["outcome"]
        path.append(
            [
                step["key"],
                outcome
                if status == WorkflowStepInstanceStatuses.COMPLETED and outcome
                else status,
            ]
        )
    return path


def _messages(result, spec):
    return [
        m
        for m in result["messages"]
        if (not spec.get("step") or m["step"] == spec["step"])
        and (not spec.get("trigger") or m["trigger"] == spec["trigger"])
        and (not spec.get("actor") or m["audience"]["actor"] == spec["actor"])
        and (
            not spec.get("recipient") or m["audience"]["recipient"] == spec["recipient"]
        )
    ]


def step_label(call_step, state, audience):
    """The label ``audience``'s dashboard shows for a step in ``state`` (a
    dry run's step state), or None where it falls back to the computed one."""
    return status_labels.pick(
        status_labels.configured(call_step, audience),
        state["status"],
        state["outcome"],
        changes_requested=state["changes_requested"],
    )


def _check(passed, assertion, message="", actual=None, step=None):
    return {
        "assertion": assertion,
        "passed": bool(passed),
        "actual": actual,
        "step": step,
        "message": "" if passed else message,
    }


def evaluate_assertion(result, assertion, call_steps, views) -> dict:
    kind = next((k for k in ASSERTION_KINDS if k in assertion), None)
    if kind == "proposal_state":
        actual = result["proposal_state"]
        return _check(
            actual == assertion["proposal_state"],
            assertion,
            f"The proposal ends {actual}, expected {assertion['proposal_state']}.",
            actual,
        )
    if kind == "step_status":
        wanted = assertion["step_status"]
        key = wanted.get("step")
        state = _latest(result, key)
        if state is None:
            return _check(False, assertion, f"There is no step '{key}'.", step=key)
        ok = state["status"] == wanted.get("status") and (
            "outcome" not in wanted or state["outcome"] == wanted["outcome"]
        )
        actual = {"status": state["status"], "outcome": state["outcome"]}
        return _check(
            ok,
            assertion,
            f"{state['name']} is {state['status']}"
            + (f" ({state['outcome']})" if state["outcome"] else "")
            + f", expected {wanted.get('status')}"
            + (f" ({wanted['outcome']})" if wanted.get("outcome") else "")
            + ".",
            actual,
            key,
        )
    if kind in ("notified", "not_notified"):
        spec = assertion[kind]
        found = _messages(result, spec)
        count = len(found)
        if kind == "not_notified":
            ok = count == 0
            message = f"{count} mail(s) match, expected none."
        elif "count" in spec:
            ok = count == spec["count"]
            message = f"{count} mail(s) match, expected {spec['count']}."
        else:
            ok = count >= spec.get("min", 1)
            message = f"{count} mail(s) match, expected at least {spec.get('min', 1)}."
        return _check(ok, assertion, message, count, spec.get("step"))
    if kind == "label":
        wanted = assertion["label"]
        key = wanted.get("step")
        state = _latest(result, key)
        call_step = call_steps.get(key)
        if state is None or call_step is None:
            return _check(False, assertion, f"There is no step '{key}'.", step=key)
        audience = wanted.get("audience") or status_labels.EVALUATORS
        role = wanted.get("role")
        if role:
            # What the role's own dashboard reads, through the real endpoint.
            view = views.get(role) or {"error": f"No preview as '{role}'."}
            if "error" in view:
                return _check(False, assertion, view["error"], step=key)
            text = view["labels"].get(key)
            audience = role
        else:
            text = step_label(call_step, state, audience)
        return _check(
            same_text(text, wanted.get("text")),
            assertion,
            f"The {audience} dashboard shows {text!r}, expected "
            f"{wanted.get('text')!r}.",
            text,
            key,
        )
    if kind in ("visible", "hidden"):
        wanted = assertion[kind]
        key = wanted.get("step")
        view = views.get(wanted.get("role"))
        if view is None:
            return _check(
                False, assertion, f"No preview as '{wanted.get('role')}'.", step=key
            )
        if "error" in view:
            return _check(False, assertion, view["error"], step=key)
        seen = key in view["steps"]
        ok = seen if kind == "visible" else not seen
        return _check(
            ok,
            assertion,
            f"{wanted.get('role')} "
            + ("does not see" if kind == "visible" else "sees")
            + f" the step '{key}'.",
            seen,
            key,
        )
    if kind == "ends_at":
        key = assertion["ends_at"]
        ended = [
            entry
            for entry in path_of(result)
            if entry[1]
            not in (
                WorkflowStepInstanceStatuses.SKIPPED,
                WorkflowStepInstanceStatuses.WITHDRAWN,
            )
        ]
        last = ended[-1][0] if ended else None
        running = result["proposal_state"] in (
            ProposalStates.IN_REVIEW,
            ProposalStates.SUBMITTED,
            ProposalStates.DRAFT,
        )
        ok = last == key and not running
        return _check(
            ok,
            assertion,
            "The workflow is still running."
            if running
            else f"The workflow ends at '{last}', expected '{key}'.",
            last,
            key,
        )
    return _check(False, assertion, "Unknown assertion.")


def _view_roles(scenario) -> list[str]:
    roles = []
    for assertion in scenario.get("assertions") or []:
        for kind in ("visible", "hidden", "label"):
            if kind in assertion:
                role = (assertion[kind] or {}).get("role")
                if role and role not in roles:
                    roles.append(role)
    return roles


def _view(view_as, applicant):
    """What the viewer reads of each step in their step list: the keys of
    the steps it shows them -- to the applicant team, those it does not keep
    behind "Internal check" -- and the label their dashboard shows for each,
    the latest run's where a step runs again after the award."""
    response = (view_as or {}).get("responses", {}).get("workflow_states") or {}
    if response.get("status") != 200:
        return {"steps": set(), "labels": {}}
    seen, labels = set(), {}
    for row in response.get("data") or []:
        key = row.get("step")
        if not key:
            continue
        if not applicant or row.get("applicant_visible"):
            seen.add(key)
        labels[key] = row.get("status_label")
    return {"steps": seen, "labels": labels}


def _request_data(call, scenario):
    """The dry-run request of ``scenario`` on ``call``: its first accepted
    offerings, as many as the scenario requests."""
    request = scenario.get("request") or {}
    wanted = request.get("offerings")
    data = {
        "actions": list(request.get("actions") or []),
    }
    if wanted is not None:
        accepted = list(
            models.RequestedOffering.objects.filter(
                call=call, state=RequestedOfferingStates.ACCEPTED
            )
            .order_by("created", "id")
            .values_list("uuid", flat=True)[:wanted]
        )
        if len(accepted) < wanted:
            raise ScenarioCannotRun(
                f"The scenario requests {wanted} offering(s); the call has "
                f"accepted {len(accepted)}."
            )
        data["requested_offerings"] = [u.hex for u in accepted]
    return data


class ScenarioCannotRun(Exception):
    pass


def run_scenario(call, scenario) -> dict:
    """Dry-run ``scenario`` on ``call`` and check its assertions."""
    base = {
        "key": scenario.get("key"),
        "name": scenario.get("name", ""),
        "status": ERROR,
        "error": None,
        "action": None,
        "failure": None,
        "assertions": [],
        "path": [],
        "proposal_state": None,
        "exercised": [],
        "tried": {},
    }
    started = time.monotonic()
    try:
        data = _request_data(call, scenario)
        result = dry_run.run(call, data)
        views = {}
        call_steps = {
            s.key: s for s in models.CallWorkflowStep.objects.filter(call=call)
        }
        for role in _view_roles(scenario):
            try:
                previewed = dry_run.run(call, {**data, "view_as": {"role": role}})
                views[role] = _view(
                    previewed.get("view_as"),
                    applicant=role == workflow_description.APPLICANT,
                )
            except dry_run.DryRunError as error:
                views[role] = {"error": error.message}
    except ScenarioCannotRun as error:
        return {**base, "error": str(error)}
    except dry_run.DryRunError as error:
        return {**base, "error": error.message, "action": error.action}
    except dry_run.DryRunBusy:
        return {**base, "error": BUSY}
    except dry_run.DryRunTimeout:
        return {**base, "error": "The scenario took too long."}
    except SoftTimeLimitExceeded:
        # The whole batch is out of time, not this scenario.
        raise
    except Exception:
        # Logged with any armed tripwire out of the way.
        with tripwires.disarmed():
            logger.exception(
                "Scenario %s of call %s failed", scenario.get("key"), call.pk
            )
        return {**base, "error": "The scenario failed. Nothing was sent or saved."}
    checks = [
        evaluate_assertion(result, assertion, call_steps, views)
        for assertion in scenario.get("assertions") or []
    ]
    failure = next((c for c in checks if not c["passed"]), None)
    coverage = result["coverage"]
    return {
        **base,
        "status": FAILED if failure else PASSED,
        "failure": failure,
        "assertions": checks,
        "path": path_of(result),
        "proposal_state": result["proposal_state"],
        "exercised": coverage["exercised"],
        "tried": {key: entry["tried"] for key, entry in coverage["outcomes"].items()},
        "seconds": round(time.monotonic() - started, 2),
    }


def scenario_totals(results) -> dict:
    return {
        "total": len(results),
        PASSED: sum(1 for r in results if r["status"] == PASSED),
        FAILED: sum(1 for r in results if r["status"] == FAILED),
        ERROR: sum(1 for r in results if r["status"] == ERROR),
    }


def coverage(description, results) -> dict:
    """What the scenarios never exercise: enabled steps no scenario reaches,
    and outcomes of the reached ones no scenario takes."""
    exercised = set()
    tried = {}
    for result in results:
        exercised.update(result.get("exercised") or [])
        for key, outcomes in (result.get("tried") or {}).items():
            tried.setdefault(key, set()).update(outcomes)
    steps = [s for s in description["steps"] if s["enabled"]]
    return {
        "steps": len(steps),
        "exercised": [s["key"] for s in steps if s["key"] in exercised],
        "unexercised": [s["key"] for s in steps if s["key"] not in exercised],
        "untried_outcomes": {
            s["key"]: [
                o["value"] for o in s["outcomes"] if o["value"] not in tried[s["key"]]
            ]
            for s in steps
            if s["key"] in tried
            and any(o["value"] not in tried[s["key"]] for o in s["outcomes"])
        },
    }


# =============================================================================
# Results kept on a call
# =============================================================================


def run_status(checks) -> tuple[str, str | None]:
    """``(status, requested_at)`` of the checks of a version: running while
    a run asked for on demand has not yet replaced them, unless it was asked
    for too long ago."""
    requested_at = (checks or {}).get("requested_at")
    if not requested_at:
        return IDLE, None
    try:
        asked = datetime.fromisoformat(requested_at)
    except (TypeError, ValueError):
        return IDLE, None
    if timezone.now() - asked > CHECK_RUN_TIMEOUT:
        return IDLE, requested_at
    return RUNNING, requested_at


def stored_scenario_checks(owner) -> tuple[models.WorkflowVersion | None, dict]:
    """The latest version of ``owner`` holding scenario results, with its
    checks: a later version may say only that a run was asked for."""
    for version in workflow_versions.owner_versions(owner).order_by("-number"):
        if (version.checks or {}).get("scenarios"):
            return version, version.checks
    return None, {}


# =============================================================================
# Running on a call
# =============================================================================


def call_scenarios(call) -> list:
    """The scenarios a call is checked against: its template's."""
    template = call.template
    if template is None:
        return []
    return list(template.scenarios or [])


def scenario_run_status(call) -> tuple[str, str | None]:
    """``(status, requested_at)`` of the call's scenarios: running while a
    run asked for has not finished, unless it was asked for too long ago."""
    for version in workflow_versions.owner_versions(call).order_by("-number"):
        requested_at = (version.checks or {}).get(SCENARIOS_REQUESTED_AT)
        if requested_at:
            return run_status({"requested_at": requested_at})
    return IDLE, None


def scenarios_error(call) -> str | None:
    """Why the last run's results were not kept, if they were not."""
    version = workflow_versions.owner_versions(call).order_by("-number").first()
    return (version.checks or {}).get(SCENARIOS_ERROR) if version else None


def request_call_scenarios(call, user) -> None:
    """Run the call's scenarios in the background, unless a run asked for
    earlier is still on its way: the call's latest version says a run is
    asked for until it has finished."""
    with transaction.atomic():
        workflow_versions.ensure_baseline(call, user)
        version = workflow_versions.owner_versions(call).order_by("-number").first()
        if version is None:
            return
        version = models.WorkflowVersion.objects.select_for_update().get(pk=version.pk)
        status, _ = scenario_run_status(call)
        if status == RUNNING:
            return
        checks = dict(version.checks or {})
        checks[SCENARIOS_REQUESTED_AT] = timezone.now().isoformat()
        models.WorkflowVersion.objects.filter(pk=version.pk).update(checks=checks)
        call_uuid = call.uuid.hex
        transaction.on_commit(
            lambda: tasks.run_call_workflow_scenarios.delay(call_uuid)
        )


def _finish_call_scenarios(call, checks=None, error=None):
    """Keep ``checks`` on the call's latest version, or say why none were
    kept, and mark the run asked for as finished."""
    with transaction.atomic():
        versions = list(
            models.WorkflowVersion.objects.select_for_update().filter(
                pk__in=workflow_versions.owner_versions(call).values("pk")
            )
        )
        latest = max(versions, key=lambda v: v.number, default=None)
        for version in versions:
            stored = dict(version.checks or {})
            changed = stored.pop(SCENARIOS_REQUESTED_AT, None) is not None
            if version is latest:
                stored.pop(SCENARIOS_ERROR, None)
                stored.update(checks or {})
                if error:
                    stored[SCENARIOS_ERROR] = error
                changed = True
            if changed:
                models.WorkflowVersion.objects.filter(pk=version.pk).update(
                    checks=stored
                )
    return latest


def run_call_scenarios(call, user=None) -> dict:
    """Run the template's scenarios on the call and keep the results on the
    call's latest version.

    Results are kept only when every scenario could run: a scenario that
    found another dry run of the call in progress -- or a batch stopped for
    taking too long -- leaves the earlier results in place, with a note
    saying why."""
    scenarios = call_scenarios(call)
    scenarios = scenarios[:MAX_SCENARIOS]
    workflow_versions.ensure_baseline(call, user)
    try:
        results = [run_scenario(call, scenario) for scenario in scenarios]
    except SoftTimeLimitExceeded:
        _finish_call_scenarios(call, error=TOO_LONG_NOT_KEPT)
        return {"error": TOO_LONG_NOT_KEPT}
    if any(result.get("error") == BUSY for result in results):
        _finish_call_scenarios(call, error=BUSY_NOT_KEPT)
        return {"error": BUSY_NOT_KEPT}
    description = workflow_description.describe_call(call)
    checks = {
        "schema": CHECKS_SCHEMA,
        "checked_at": timezone.now().isoformat(),
        "scenarios": results,
        "totals": {"scenarios": scenario_totals(results)},
        "coverage": coverage(description, results),
    }
    version = _finish_call_scenarios(call, checks)
    if version is not None:
        checks = {**checks, "version": version.number}
    return checks


# =============================================================================
# Coverage of the paths proposals take
# =============================================================================


def _scenario_paths(call) -> Iterable | None:
    """The paths the scenarios of ``call`` walked when last run on it."""
    _, checks = stored_scenario_checks(call)
    results = checks.get("scenarios") or []
    walked = [r["path"] for r in results if r.get("status") == PASSED and r["path"]]
    return walked or None


workflow_observability.SCENARIO_PATH_SOURCES.append(_scenario_paths)


# =============================================================================
# Turning a dry run into a scenario
# =============================================================================


def scenario_draft(call, data, result) -> dict:
    """A scenario that replays ``data``'s path on ``call`` and asserts where
    it ended: the proposal's state, every step it reached, and the mail each
    audience got. The offerings are named by how many, as a scenario runs on
    the call's first accepted ones."""
    requested = data.get("requested_offerings")
    assertions = [{"proposal_state": result["proposal_state"]}]
    latest = {}
    for step in result["steps"]:
        latest[step["key"]] = step
    for key, step in latest.items():
        if step["status"] == WorkflowStepInstanceStatuses.PENDING:
            continue
        entry = {"step": key, "status": step["status"]}
        if step["outcome"]:
            entry["outcome"] = step["outcome"]
        assertions.append({"step_status": entry})
    counts = {}
    for message in result["messages"]:
        actor = message["audience"]["actor"]
        if actor:
            counts[actor] = counts.get(actor, 0) + 1
    for actor, count in sorted(counts.items()):
        assertions.append({"notified": {"actor": actor, "count": count}})
    actions = []
    for action in data.get("actions") or []:
        actions.append(
            {name: value for name, value in action.items() if value not in (None, "")}
        )
    return {
        "key": "",
        "name": "",
        "description": "",
        "request": {
            "offerings": len(requested)
            if requested is not None
            else len(result["requested_offerings"]),
            "actions": actions,
        },
        "assertions": assertions[:MAX_ASSERTIONS],
    }
