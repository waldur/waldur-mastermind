"""Checking a call's workflow before it runs.

A registry of small rules over the workflow description
(``workflow_description.describe``). Each rule returns findings::

    {code, title, severity, step, step_name, field, message, because}

``title`` names the rule in a few words, for a list of findings; ``message``
says in full what is wrong on this call.

``step`` is the key of the step the finding is about, or None for the call;
``field`` is the setting that fixes it -- a step field, or a call-level
section (``rounds``, ``offerings``, ``panel_chair``, ...) -- so the UI can
open it. Severities: ``error`` (the call cannot run as configured: it is not
activated), ``warning`` (it runs, likely not as intended) and ``info``.

Structural rules read the document only and also run on templates; live
rules read the call (its rounds, offerings, budgets, people as counts) and
run on calls only.
"""

import dataclasses
from collections.abc import Callable

from django.db.models import Count
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.proposal import (
    call_transfer,
    models,
    provider_calls,
    serializers,
    utils,
    workflow_diff,
    workflow_versions,
)
from waldur_mastermind.proposal import workflow_description as wd
from waldur_mastermind.proposal.enums import (
    AWARD_RESPONSE_KEY,
    AutomaticDecisionGuards,
    BudgetEnforcement,
    ContributionOutcomes,
    ContributorSources,
    DelegationTargets,
    EvaluationStart,
    OnNegativeOutcomes,
    OutcomeSets,
    ProposalRoleEligibility,
    RequestedOfferingStates,
    ResponsibleRoles,
    StepKinds,
    TransitionModes,
    WorkflowPhases,
    WorkflowStepOutcomes,
    parse_delegation_target,
)

ERROR = "error"
WARNING = "warning"
INFO = "info"
SEVERITIES = (ERROR, WARNING, INFO)

# Call-level fields a finding can point at, besides the call's own settings.
CALL_ROUNDS = "rounds"
CALL_OFFERINGS = "offerings"
CALL_WORKFLOW_STEPS = "workflow_steps"
CALL_PROPOSAL_ROLES = "proposal_roles"
CALL_PANELS = "panels"
CALL_BUDGETS = "budgets"
CALL_REVIEWER_POOL = "reviewer_pool"

# Evaluator role eligibility -> the description actor holding the call role.
ELIGIBILITY_ACTORS = {
    ProposalRoleEligibility.CALL_MANAGER: wd.CALL_MANAGER,
    ProposalRoleEligibility.CALL_REVIEWER: wd.REVIEWER,
    ProposalRoleEligibility.CALL_PANEL_MEMBER: wd.PANEL_MEMBER,
}
ELIGIBILITY_HOLDERS = {
    wd.CALL_MANAGER: "call managers",
    wd.REVIEWER: "reviewers",
    wd.PANEL_MEMBER: "panel members",
}
PANEL_CHAIR_ELIGIBILITY = "panel:chair"
PANEL_MEMBER_ELIGIBILITY = "panel:member"
PANEL_ELIGIBILITIES = (PANEL_CHAIR_ELIGIBILITY, PANEL_MEMBER_ELIGIBILITY)


@dataclasses.dataclass
class Rule:
    code: str
    severity: str
    title: str
    function: Callable
    live: bool = False


RULES: list[Rule] = []


def rule(code, severity, title, live=False):
    """Register ``function(context)`` returning findings for ``code``, named
    ``title`` in a list of findings."""

    def register(function):
        RULES.append(Rule(code, severity, title, function, live))
        return function

    return register


def title(code) -> str:
    """The short name of the rule ``code``."""
    for registered in RULES:
        if registered.code == code:
            return str(registered.title)
    return str(FLAG_TITLES[code])


class Context:
    """What the rules read: the document, its description, and on a call the
    call itself."""

    def __init__(self, document, call=None):
        self.document = document if isinstance(document, dict) else {}
        self.call = call
        self.live = wd.live_context(call) if call is not None else None
        self.describer = wd._Describer(self.document, self.live)
        self.description = self.describer.describe()
        self.steps = self.description["steps"]
        self.by_key = {s["key"]: s for s in self.steps}
        self.raw = self.describer.by_key
        self.roles = self.describer.role_by_key
        self.enabled = [s for s in self.steps if s["enabled"]]
        self.code: str | None = None
        self.severity: str | None = None

    # what the rules produce ----------------------------------------------

    def finding(self, step, field, message, because=(), severity=None, code=None):
        name = None
        if step is not None:
            described = self.by_key.get(step)
            name = described["name"] if described else step
        code = code or self.code
        return {
            "code": code,
            "title": title(code),
            "severity": severity or self.severity,
            "step": step,
            "step_name": name,
            "field": field,
            "message": str(message),
            "because": list(because),
        }

    # reading the workflow ------------------------------------------------

    def role(self, key):
        return self.roles.get(key) if key else None

    def step_role(self, step):
        """The evaluator role key a step's contributor source names."""
        return wd._role_key(self.raw[step["key"]].get("contributor_source"))

    def earlier(self, step):
        """Enabled steps of stages before ``step``'s."""
        return [s for s in self.enabled if s["stage"] < step["stage"]]

    def ends_negatively(self, step):
        """Whether a negative outcome of ``step`` ends the proposal's
        workflow (``CallWorkflowStep.can_end_negatively``)."""
        return (
            step["kind"] in (StepKinds.ASSESSMENT, StepKinds.DECISION)
            and step["on_negative"] != OnNegativeOutcomes.CONTINUE
            and any(o["polarity"] == "negative" for o in step["outcomes"])
        )

    def condition_outcomes(self, step):
        """Outcomes a run condition may name on ``step``
        (``CallWorkflowStep.condition_outcomes``)."""
        outcomes = [o["value"] for o in step["outcomes"]]
        outcomes += list(
            ContributionOutcomes.for_step(
                step["outcome_set"], step.get("allows_conditional_acceptance", True)
            )
        )
        outcomes.append(WorkflowStepOutcomes.EXPIRED)
        return list(dict.fromkeys(outcomes))

    def assigning_steps(self, role_key):
        return [s for s in self.enabled if role_key in s["assigns_roles"]]

    def holders(self, actor):
        if self.live is None:
            return None
        return self.live.holders.get(actor)


def _ref(source, field, step=None, detail=None):
    return wd.ref(source, field, step, detail)


def _sort_key(context):
    order = {s["key"]: index for index, s in enumerate(context.steps)}

    def key(finding):
        return (
            SEVERITIES.index(finding["severity"]),
            finding["step"] is not None,
            order.get(finding["step"], -1),
            finding["code"],
        )

    return key


def check(document, call=None) -> list[dict]:
    """Findings over the workflow ``document`` configures; with ``call``, the
    live rules too."""
    context = Context(document, call)
    findings = []
    for registered in RULES:
        if registered.live and call is None:
            continue
        context.code, context.severity = registered.code, registered.severity
        findings.extend(registered.function(context) or [])
    findings.sort(key=_sort_key(context))
    return findings


def call_document(call) -> dict:
    """The call's document as the checker reads it: settings, workflow and
    rounds."""
    document, _warnings = call_transfer.export_call(
        call, {"documents": False, "offerings": False}
    )
    return document


def check_call(call) -> list[dict]:
    return check(call_document(call), call)


def errors(findings) -> list[dict]:
    return [f for f in findings if f["severity"] == ERROR]


def summary(findings) -> dict:
    return {
        severity: sum(1 for f in findings if f["severity"] == severity)
        for severity in SEVERITIES
    }


# -- what activation always refused --------------------------------------------


@rule("no_round", ERROR, gettext_lazy("No round"), live=True)
def no_round(context):
    if not context.call.round_set.exists():
        return [
            context.finding(
                None, CALL_ROUNDS, _("Call must have a round to be activated.")
            )
        ]


@rule("no_enabled_step", ERROR, gettext_lazy("No enabled step"))
def no_enabled_step(context):
    if not context.enabled:
        return [
            context.finding(
                None,
                CALL_WORKFLOW_STEPS,
                _("Call must have at least one enabled workflow step."),
            )
        ]


def _decisions(context):
    return [
        s
        for s in context.enabled
        if s["is_allocation_decision"] and s["kind"] == StepKinds.DECISION
    ]


@rule("decision_missing", ERROR, gettext_lazy("No allocation decision"))
def decision_missing(context):
    if not context.enabled or _decisions(context):
        return []
    # Said apart: a decision step that is there but switched off is turned
    # back on; one that is not there at all is added.
    disabled = [
        s
        for s in context.steps
        if not s["enabled"]
        and s["is_allocation_decision"]
        and s["kind"] == StepKinds.DECISION
    ]
    if disabled:
        return [
            context.finding(
                disabled[0]["key"],
                CALL_WORKFLOW_STEPS,
                _(
                    "The allocation decision step is disabled. Every call "
                    "needs one: enable it."
                ),
            )
        ]
    return [
        context.finding(
            None,
            CALL_WORKFLOW_STEPS,
            _(
                "The call has no allocation decision step. Every call needs "
                "one: add it."
            ),
        )
    ]


@rule("decision_multiple", ERROR, gettext_lazy("Several allocation decisions"))
def decision_multiple(context):
    decisions = _decisions(context)
    if len(decisions) > 1:
        message = _(
            "Call must have exactly one enabled allocation decision "
            "step; it has %(count)s."
        ) % {"count": len(decisions)}
        return [
            context.finding(
                s["key"],
                "is_allocation_decision",
                message,
                [_ref("step", "is_allocation_decision", s["key"])],
            )
            for s in decisions
        ]


@rule(
    "award_before_decision", ERROR, gettext_lazy("Award response before the decision")
)
def award_before_decision(context):
    decisions = [
        s["order"] or 0 for s in context.enabled if s["is_allocation_decision"]
    ]
    responses = [s for s in context.enabled if s["key"] == AWARD_RESPONSE_KEY]
    if decisions and responses and (responses[0]["order"] or 0) < min(decisions):
        return [
            context.finding(
                AWARD_RESPONSE_KEY,
                "order",
                serializers.AWARD_BEFORE_DECISION,
                [_ref("step", "order", AWARD_RESPONSE_KEY)],
            )
        ]


@rule(
    "automatic_decision_unguarded", ERROR, gettext_lazy("Unguarded automatic decision")
)
def automatic_decision_unguarded(context):
    for step in context.enabled:
        if step["is_allocation_decision"] and step["decides_automatically"]:
            if _relies_on_provider_fulfilment(step):
                # Checked on the call's offerings: automatic_decision_unattended.
                return []
            return [
                context.finding(
                    step["key"],
                    "decides_automatically",
                    serializers.UNGUARDED_AUTOMATIC_DECISION,
                    [_ref("step", "decides_automatically", step["key"])],
                )
            ]
        if context.ends_negatively(step):
            return []


def _relies_on_provider_fulfilment(step):
    return (
        step["decides_automatically"]
        and step["automatic_decision_guard"]
        == AutomaticDecisionGuards.PROVIDER_FULFILMENT
    )


@rule(
    "automatic_decision_unattended",
    ERROR,
    gettext_lazy("Orders fulfilled unattended"),
    live=True,
)
def automatic_decision_unattended(context):
    """An automatic decision relying on the provider fulfilling each order,
    on a call with an accepted offering whose orders go through with nobody
    at the provider acting on them: an award there would be provisioned
    unchecked."""
    for step in context.enabled:
        if not (
            step["is_allocation_decision"] and _relies_on_provider_fulfilment(step)
        ):
            continue
        unattended = utils.offerings_fulfilled_unattended(context.call)
        if not unattended:
            return []
        return [
            context.finding(
                step["key"],
                "automatic_decision_guard",
                str(utils.OFFERINGS_FULFILLED_UNATTENDED)
                % {"offerings": ", ".join(o.name for o in unattended)},
                [
                    _ref("step", "automatic_decision_guard", step["key"]),
                    _ref("call", CALL_OFFERINGS),
                ],
            )
        ]
    return []


@rule(
    "applicant_step_without_form", ERROR, gettext_lazy("Applicant step without a form")
)
def applicant_step_without_form(context):
    # An applicant step that ends in a submission is answered through its
    # form: without one the applicant has nothing to respond with.
    return [
        context.finding(
            s["key"],
            "checklist",
            _(
                "These applicant steps have no form, so the applicant would "
                "have nothing to submit: %s. Attach a form to each."
            )
            % s["name"],
            [_ref("step", "checklist", s["key"])],
        )
        for s in context.enabled
        if s["kind"] == StepKinds.APPLICANT_ACTION
        and s["outcome_set"] == OutcomeSets.SUBMITTED
        and s["checklist"] is None
    ]


@rule("no_accepted_offering", ERROR, gettext_lazy("No accepted offering"), live=True)
def no_accepted_offering(context):
    accepted = context.call.requestedoffering_set.filter(
        state=RequestedOfferingStates.ACCEPTED
    )
    if not accepted.exists():
        return [
            context.finding(
                None,
                CALL_OFFERINGS,
                _("Call must have at least one accepted offering to be activated."),
            )
        ]


@rule(
    "offering_without_plan", ERROR, gettext_lazy("Offering without a plan"), live=True
)
def offering_without_plan(context):
    # Applicants request only offerings with a plan; a plan can only be set
    # while the offering is still requested, so this is the last point it is
    # still fixable.
    planless = list(
        context.call.requestedoffering_set.filter(
            state=RequestedOfferingStates.ACCEPTED, plan__isnull=True
        ).values_list("offering__name", flat=True)
    )
    if planless:
        return [
            context.finding(
                None,
                CALL_OFFERINGS,
                _("These offerings have no plan and could not be requested: %s.")
                % ", ".join(planless),
            )
        ]


@rule(
    "offering_not_orderable",
    ERROR,
    gettext_lazy("Offering no longer orderable"),
    live=True,
)
def offering_not_orderable(context):
    """An accepted offering its provider has since archived, sent back to
    draft or made unavailable: allocation can order nothing on it."""
    stopped = [
        f"{offering.name} ({offering.get_state_display().lower()})"
        for offering in marketplace_models.Offering.objects.filter(
            requestedoffering__call=context.call,
            requestedoffering__state=RequestedOfferingStates.ACCEPTED,
        )
        .exclude(state__in=(OfferingStates.ACTIVE, OfferingStates.PAUSED))
        .distinct()
        .order_by("name")
    ]
    if stopped:
        return [
            context.finding(
                None,
                CALL_OFFERINGS,
                _(
                    "These offerings no longer take orders, so awards on them "
                    "cannot be provisioned: %s. Ask their providers, or remove "
                    "them from the call."
                )
                % ", ".join(stopped),
            )
        ]


@rule("plan_archived", ERROR, gettext_lazy("Plan archived"), live=True)
def plan_archived(context):
    """An accepted offering whose plan the provider archived: awards on it
    cannot be ordered until the call takes another plan."""
    archived = list(
        context.call.requestedoffering_set.filter(
            state=RequestedOfferingStates.ACCEPTED, plan__archived=True
        )
        .order_by("offering__name")
        .values_list("offering__name", "plan__name")
    )
    if archived:
        return [
            context.finding(
                None,
                CALL_OFFERINGS,
                _(
                    "These offerings' plans have been archived, so awards on "
                    "them cannot be provisioned: %s. Choose another plan."
                )
                % ", ".join(f"{offering} ({plan})" for offering, plan in archived),
            )
        ]


@rule(
    "component_rule_orphaned",
    ERROR,
    gettext_lazy("Component gone from offering"),
    live=True,
)
def component_rule_orphaned(context):
    """A component rule or pool of the call whose component the offering no
    longer has: it bounds nothing, and requests in it cannot be ordered."""
    orphaned = []
    for rule_ in models.CallOfferingComponent.objects.filter(
        requested_offering__call=context.call
    ).select_related("requested_offering__offering"):
        offering = rule_.requested_offering.offering
        if not offering.components.filter(type=rule_.component_type).exists():
            orphaned.append(f"{offering.name}: {rule_.component_type}")
    if orphaned:
        return [
            context.finding(
                None,
                CALL_OFFERINGS,
                _(
                    "These components are gone from their offerings, so the "
                    "call's limits and pools on them bound nothing: %s."
                )
                % ", ".join(sorted(orphaned)),
            )
        ]


# -- who can do the work ---------------------------------------------------------


def _offering_team_steps(context):
    """Enabled steps the offering managers complete or each requested
    offering's team contributes to."""
    return [
        s
        for s in context.enabled
        if s["completer"] == ResponsibleRoles.OFFERING_MANAGER
        or s["contributors"]["source"] == ContributorSources.REQUESTED_OFFERING_TEAM
    ]


def _provider_contact(customer):
    if customer is None:
        return ""
    if customer.email:
        return f"{customer.name} ({customer.email})"
    return customer.name


@rule(
    "offering_without_managers",
    WARNING,
    gettext_lazy("Offering without managers"),
    live=True,
)
def offering_without_managers(context):
    """An offering in the call whose team the workflow hands work to, with
    nobody on that team. The call's managers cannot invite them: the
    offering's provider adds offering managers in the marketplace, so the
    finding names the provider to ask."""
    steps = _offering_team_steps(context)
    if not steps:
        return []
    requested = list(
        context.call.requestedoffering_set.exclude(
            state=RequestedOfferingStates.CANCELED
        )
        .select_related("offering__customer")
        .order_by("offering__name", "id")
    )
    counts = provider_calls.offering_manager_counts({r.offering_id for r in requested})
    step_names = ", ".join(s["name"] for s in steps)
    findings = []
    seen = set()
    for requested_offering in requested:
        offering = requested_offering.offering
        if counts.get(offering.pk) or offering.pk in seen:
            continue
        seen.add(offering.pk)
        findings.append(
            context.finding(
                None,
                CALL_OFFERINGS,
                _(
                    "Nobody can do the offering team's part for '%(offering)s': "
                    "it has no offering manager, and the team acts in these "
                    "steps: %(steps)s. The call cannot invite them; ask its "
                    "provider, %(provider)s, to add offering managers on the "
                    "offering's Team tab."
                )
                % {
                    "offering": offering.name,
                    "steps": step_names,
                    "provider": _provider_contact(offering.customer),
                },
                [
                    _ref("call", "requested_offerings", detail=offering.uuid.hex),
                    *(
                        _ref("step", "completer", s["key"])
                        if s["completer"] == ResponsibleRoles.OFFERING_MANAGER
                        else _ref("step", "contributor_source", s["key"])
                        for s in steps
                    ),
                ],
            )
        )
    return findings


def _holdable(context, role_key, step):
    """Why nobody can hold the evaluator role ``role_key`` when ``step`` runs,
    as ``(severity, message, because)``; None when someone can."""
    role = context.role(role_key)
    if role is None:
        return (
            ERROR,
            _("The evaluator role '%s' is not one of the call's.") % role_key,
            [_ref("call", CALL_PROPOSAL_ROLES)],
        )
    name = role.get("name") or role_key
    eligible = role.get("eligible_from")
    if eligible == ProposalRoleEligibility.PROPOSAL_REVIEWERS and not any(
        s["contributors"]["source"] == ContributorSources.ASSIGNED_REVIEWERS
        for s in context.earlier(step)
    ):
        return (
            ERROR,
            _(
                "The evaluator role '%s' is held by reviewers who reviewed the "
                "proposal, and no earlier step has the assigned reviewers "
                "review it."
            )
            % name,
            [_ref("call", "eligible_from", detail=role_key)],
        )
    if eligible in PANEL_ELIGIBILITIES and not _panels(context):
        # The domain-panel rules report it, once per role.
        return None
    assigning = context.assigning_steps(role_key)
    late = [s for s in assigning if s["stage"] >= step["stage"]]
    if (
        assigning
        and len(late) == len(assigning)
        and step["kind"] != StepKinds.ASSIGNMENT
    ):
        return (
            ERROR,
            _(
                "The evaluator role '%(role)s' is assigned by step "
                "'%(assignment)s' only after this step runs."
            )
            % {"role": name, "assignment": late[0]["name"]},
            [_ref("step", "assigns_roles", late[0]["key"])],
        )
    actor = ELIGIBILITY_ACTORS.get(eligible)
    if actor is not None and context.holders(actor) == 0:
        return (
            WARNING,
            _(
                "The evaluator role '%(role)s' is held by the call's %(who)s, and "
                "the call has none yet."
            )
            % {"role": name, "who": ELIGIBILITY_HOLDERS[actor]},
            [_ref("call", "eligible_from", detail=role_key)],
        )
    return None


@rule("completer_unholdable", ERROR, gettext_lazy("Nobody can complete the step"))
def completer_unholdable(context):
    findings = []
    for step in context.enabled:
        key = step["key"]
        completer = step["completer"]
        if (
            completer == ResponsibleRoles.PANEL_CHAIR
            and context.live is not None
            and not context.holders(wd.PANEL_CHAIR)
        ):
            findings.append(
                context.finding(
                    key,
                    "completer",
                    _(
                        "The step is completed by the panel chair, and the call "
                        "has no chair or vice-chair."
                    ),
                    [_ref("step", "completer", key), _ref("call", "panel_chair")],
                )
            )
        if completer != ResponsibleRoles.PROPOSAL_ROLE:
            continue
        role_key = step["completer_proposal_role"]
        if step["completion_lead_only"] and not (context.role(role_key) or {}).get(
            "allows_lead"
        ):
            findings.append(
                context.finding(
                    key,
                    "contributors_lead_only",
                    _(
                        "The step is completed by the lead of '%s', a role "
                        "without a lead."
                    )
                    % role_key,
                    [_ref("step", "contributors_lead_only", key)],
                )
            )
        problem = _holdable(context, role_key, step)
        if problem is not None:
            severity, message, because = problem
            findings.append(
                context.finding(
                    key,
                    "completer_proposal_role",
                    message,
                    [_ref("step", "completer_proposal_role", key), *because],
                    severity=severity,
                )
            )
    return findings


@rule("contributors_unholdable", ERROR, gettext_lazy("Too few possible contributors"))
def contributors_unholdable(context):
    findings = []
    for step in context.enabled:
        key = step["key"]
        contributors = step["contributors"]
        if contributors["source"] != ContributorSources.PROPOSAL_ROLE:
            continue
        role_key = contributors["parameter"]
        role = context.role(role_key)
        if (
            role is not None
            and contributors["lead_only"]
            and not role.get("allows_lead")
        ):
            findings.append(
                context.finding(
                    key,
                    "contributors_lead_only",
                    _("Only the lead of '%s' contributes, and the role has no lead.")
                    % (role.get("name") or role_key),
                    [_ref("step", "contributors_lead_only", key)],
                )
            )
        maximum = (role or {}).get("max_holders")
        if (
            role is not None
            and maximum
            and not contributors["per_requested_offering"]
            and contributors["min"]
            and contributors["min"] > maximum
        ):
            findings.append(
                context.finding(
                    key,
                    "min_contributions",
                    _(
                        "The step needs %(min)s contributions, and at most %(max)s "
                        "people hold '%(role)s'."
                    )
                    % {
                        "min": contributors["min"],
                        "max": maximum,
                        "role": role.get("name") or role_key,
                    },
                    [
                        _ref("step", "min_contributions", key),
                        _ref("call", "max_holders", detail=role_key),
                    ],
                )
            )
        if step["completer"] == ResponsibleRoles.PROPOSAL_ROLE and (
            step["completer_proposal_role"] == role_key
        ):
            # Reported once, for the completer.
            continue
        problem = _holdable(context, role_key, step)
        if problem is not None:
            severity, message, because = problem
            findings.append(
                context.finding(
                    key,
                    "contributor_source",
                    message,
                    [_ref("step", "contributor_source", key), *because],
                    severity=severity,
                )
            )
    return findings


@rule("role_assigned_by_hand", INFO, gettext_lazy("Role assigned by hand"))
def role_assigned_by_hand(context):
    """An evaluator role no step assigns: the call managers assign it on
    each proposal, by hand."""
    findings = []
    seen = set()
    for step in context.enabled:
        keys = []
        if step["completer"] == ResponsibleRoles.PROPOSAL_ROLE:
            keys.append(("completer_proposal_role", step["completer_proposal_role"]))
        if step["contributors"]["source"] == ContributorSources.PROPOSAL_ROLE:
            keys.append(("contributor_source", step["contributors"]["parameter"]))
        for field, role_key in keys:
            role = context.role(role_key)
            if (
                role is None
                or role_key in seen
                or role.get("eligible_from") in PANEL_ELIGIBILITIES
                or context.assigning_steps(role_key)
            ):
                continue
            seen.add(role_key)
            findings.append(
                context.finding(
                    step["key"],
                    field,
                    _(
                        "No step assigns '%s': the call managers assign its "
                        "holders on each proposal before this step can be done."
                    )
                    % (role.get("name") or role_key),
                    [_ref("step", field, step["key"])],
                )
            )
    return findings


@rule("delegation_unholdable", ERROR, gettext_lazy("Delegation does not fit the step"))
def delegation_unholdable(context):
    findings = []
    for step in context.enabled:
        key = step["key"]
        try:
            target, parameter = parse_delegation_target(
                context.raw[key].get("delegable_to")
            )
        except ValueError:
            continue
        per_offering = step["contributors"]["per_requested_offering"]
        if target == DelegationTargets.OFFERING_TEAM and not per_offering:
            findings.append(
                context.finding(
                    key,
                    "delegable_to",
                    _(
                        "Slots are handed to the offering's team only on a step "
                        "with a contribution per requested offering."
                    ),
                    [_ref("step", "delegable_to", key)],
                )
            )
        elif target == DelegationTargets.PROPOSAL_ROLE:
            role_key = wd._role_key(f"{wd.ROLE_PREFIX}{parameter}")
            if per_offering:
                findings.append(
                    context.finding(
                        key,
                        "delegable_to",
                        _(
                            "A per-offering step hands its slots to the offering's "
                            "team, not to an evaluator role."
                        ),
                        [_ref("step", "delegable_to", key)],
                    )
                )
                continue
            problem = _holdable(context, role_key, step)
            if problem is not None and problem[0] == ERROR:
                findings.append(
                    context.finding(
                        key,
                        "delegable_to",
                        problem[1],
                        [_ref("step", "delegable_to", key), *problem[2]],
                    )
                )
    return findings


# -- the order steps run in ---------------------------------------------------------


@rule("condition_bad_reference", ERROR, gettext_lazy("Invalid run condition"))
def condition_bad_reference(context):
    findings = []
    for step in context.steps:
        condition = step["run_condition"]
        if not condition or not step["enabled"]:
            continue
        key = step["key"]
        because = [_ref("step", "run_condition", key)]
        if step["is_allocation_decision"]:
            findings.append(
                context.finding(
                    key,
                    "run_condition",
                    _(
                        "The allocation decision runs on every proposal; it "
                        "cannot have a run condition."
                    ),
                    because,
                )
            )
            continue
        source = context.by_key.get(condition["step"])
        if source is None:
            findings.append(
                context.finding(
                    key,
                    "run_condition",
                    _(
                        "Step '%(step)s' runs on the outcome of step '%(source)s', "
                        "which the call does not have."
                    )
                    % {"step": step["name"], "source": condition["step"]},
                    because,
                )
            )
            continue
        if source["stage"] >= step["stage"]:
            findings.append(
                context.finding(
                    key,
                    "run_condition",
                    _(
                        "Step '%(step)s' runs on the outcome of step '%(source)s', "
                        "which does not end before it."
                    )
                    % {"step": step["name"], "source": source["name"]},
                    because,
                )
            )
            continue
        if condition.get("score_spread_above") is not None:
            if source["contributors"]["source"] == ContributorSources.NONE:
                findings.append(
                    context.finding(
                        key,
                        "run_condition",
                        _(
                            "Step '%(step)s' runs on how far apart the scores "
                            "of step '%(source)s' lie, which has no "
                            "contributors to score the proposal."
                        )
                        % {"step": step["name"], "source": source["name"]},
                        [*because, _ref("step", "contributor_source", source["key"])],
                    )
                )
            continue
        known = context.condition_outcomes(source)
        unknown = [o for o in condition["outcomes"] if o not in known]
        if unknown:
            findings.append(
                context.finding(
                    key,
                    "run_condition",
                    _(
                        "Step '%(source)s' cannot end with %(unknown)s; use one "
                        "of %(known)s."
                    )
                    % {
                        "source": source["name"],
                        "unknown": ", ".join(unknown),
                        "known": ", ".join(known),
                    },
                    because,
                )
            )
    return findings


@rule(
    "condition_scores_cannot_diverge",
    WARNING,
    gettext_lazy("Score condition never met"),
)
def condition_scores_cannot_diverge(context):
    """A step running on how far apart an earlier step's scores lie needs
    that step to collect two scores at least: with one, it never runs."""
    findings = []
    for step in context.enabled:
        condition = step["run_condition"] or {}
        if condition.get("score_spread_above") is None:
            continue
        source = context.by_key.get(condition.get("step"))
        if source is None:
            continue
        contributors = source["contributors"]
        if contributors["source"] == ContributorSources.NONE:
            continue
        if contributors["max"] is not None and contributors["max"] < 2:
            findings.append(
                context.finding(
                    step["key"],
                    "run_condition",
                    _(
                        "Step '%(step)s' runs only when the scores of step "
                        "'%(source)s' lie more than %(points)s points apart, "
                        "and that step takes at most one contribution: it "
                        "never runs."
                    )
                    % {
                        "step": step["name"],
                        "source": source["name"],
                        "points": condition["score_spread_above"],
                    },
                    [
                        _ref("step", "run_condition", step["key"]),
                        _ref("step", "max_contributions", source["key"]),
                    ],
                )
            )
    return findings


@rule("condition_unreachable", ERROR, gettext_lazy("Conditional step never runs"))
def condition_unreachable(context):
    findings = []
    for step in context.enabled:
        condition = step["run_condition"]
        source = context.by_key.get((condition or {}).get("step"))
        if not condition or source is None or source["stage"] >= step["stage"]:
            continue
        key = step["key"]
        outcomes = set(condition["outcomes"])
        if not outcomes:
            continue
        because = [
            _ref("step", "run_condition", key),
            _ref("step", "on_negative", source["key"]),
        ]
        if not source["enabled"]:
            findings.append(
                context.finding(
                    key,
                    "run_condition",
                    _(
                        "Step '%(step)s' runs on the outcome of step '%(source)s', "
                        "which is disabled: it never runs."
                    )
                    % {"step": step["name"], "source": source["name"]},
                    [_ref("step", "run_condition", key)],
                )
            )
            continue
        negative = {
            o["value"] for o in source["outcomes"] if o["polarity"] == "negative"
        }
        contribution = set(
            ContributionOutcomes.for_step(
                source["outcome_set"],
                source.get("allows_conditional_acceptance", True),
            )
        )
        if (
            context.ends_negatively(source)
            and outcomes <= negative
            and not outcomes & contribution
        ):
            findings.append(
                context.finding(
                    key,
                    "run_condition",
                    _(
                        "Step '%(step)s' runs only when step '%(source)s' ends "
                        "with %(outcomes)s, which ends the proposal there: it "
                        "never runs."
                    )
                    % {
                        "step": step["name"],
                        "source": source["name"],
                        "outcomes": ", ".join(sorted(outcomes)),
                    },
                    because,
                )
            )
    return findings


@rule("input_not_earlier", ERROR, gettext_lazy("Input is not an earlier step"))
def input_not_earlier(context):
    findings = []
    for step in context.enabled:
        key = step["key"]
        for input_key in step["input_steps"]:
            source = context.by_key.get(input_key)
            if source is None:
                message = _(
                    "Step '%(step)s' reads step '%(input)s', which the call does not have."
                )
            elif source["stage"] == step["stage"]:
                message = _(
                    "Step '%(step)s' reads step '%(input)s', which runs alongside "
                    "it; a step reads only earlier stages."
                )
            elif source["stage"] > step["stage"]:
                message = _(
                    "Step '%(step)s' reads step '%(input)s', which comes after "
                    "it; only earlier steps can be read."
                )
            else:
                continue
            findings.append(
                context.finding(
                    key,
                    "input_steps",
                    message
                    % {
                        "step": step["name"],
                        "input": source["name"] if source else input_key,
                    },
                    [_ref("step", "input_steps", key, detail=input_key)],
                )
            )
    return findings


@rule("stage_forbidden_member", ERROR, gettext_lazy("Step must run alone"))
def stage_forbidden_member(context):
    stages = {}
    for step in context.enabled:
        stages.setdefault(step["stage"], []).append(step)
    findings = []
    for members in stages.values():
        if len(members) < 2:
            continue
        for step in members:
            if step["is_allocation_decision"]:
                message = _(
                    "The allocation decision '%s' runs alone, not alongside "
                    "another step."
                )
            elif step["kind"] == StepKinds.APPLICANT_ACTION:
                message = _(
                    "The applicant step '%s' runs alone, not alongside another step."
                )
            else:
                continue
            findings.append(
                context.finding(
                    step["key"],
                    "parallel_with_previous",
                    message % step["name"],
                    [_ref("step", "parallel_with_previous", step["key"])],
                )
            )
    return findings


# -- notifications ---------------------------------------------------------------

# Notification flags of the description as findings: the rule codes, and the
# flags that mean a rule can never reach anyone.
FLAG_CODES = {
    "missing": "notification_missing",
    "duplicate": "notification_duplicate",
    "leak_risk": "notification_leak_risk",
    "unreachable": "notification_resolves_to_nobody",
}
# The flag codes no rule registers, by name.
FLAG_TITLES = {
    "notification_missing": gettext_lazy("Nobody is told"),
    "notification_duplicate": gettext_lazy("Told twice"),
    "notification_leak_risk": gettext_lazy("May tell the applicant too much"),
}
NEVER_REACHED_FLAGS = frozenset({"role_without_lead"})


@rule("notification_resolves_to_nobody", ERROR, gettext_lazy("Reaches nobody"))
def notification_flags(context):
    findings = []
    for flag in context.description["notifications"]["flags"]:
        step = context.by_key.get(flag["step"])
        if step is not None and not step["enabled"]:
            continue
        severity = flag["severity"]
        if flag["code"] in NEVER_REACHED_FLAGS:
            severity = ERROR
        findings.append(
            context.finding(
                flag["step"],
                "notification_rules",
                flag["message"],
                [
                    *flag["because"],
                    _ref("rule", "flag", flag["step"], detail=flag["code"]),
                    # Two rules of one step can raise the same flag; the
                    # event each reacts to tells their findings apart.
                    *(
                        [_ref("rule", "trigger", flag["step"], detail=flag["trigger"])]
                        if flag.get("trigger")
                        else []
                    ),
                ],
                severity=severity,
                code=FLAG_CODES.get(flag["kind"], "notification_missing"),
            )
        )
    for step in context.enabled:
        for notification in step["notifications"]:
            role_key = notification["proposal_role"]
            if notification["enabled"] and role_key and context.role(role_key) is None:
                findings.append(
                    context.finding(
                        step["key"],
                        "notification_rules",
                        _(
                            "A notification rule tells the holders of '%s', which "
                            "is not one of the call's evaluator roles."
                        )
                        % role_key,
                        [_ref("rule", "proposal_role", step["key"])],
                    )
                )
    return findings


# -- the schedule ---------------------------------------------------------------


@rule(
    "durations_exceed_decision_date",
    WARNING,
    gettext_lazy("Steps outlast the decision date"),
)
def durations_exceed_decision_date(context):
    """The longest path from evaluation to the decision -- a stage counts as
    its longest step, a conditional step as taken -- against each round's
    time to decision."""
    decision = next((s for s in context.enabled if s["is_allocation_decision"]), None)
    if decision is None:
        return []
    stages = context.description["schedule"]["stages"]
    path, days = [], 0
    for stage in stages:
        if stage["stage"] > decision["stage"]:
            break
        days = stage["end_day"]
        longest = max(
            (context.by_key[k] for k in stage["steps"]),
            key=lambda s: s["duration_in_days"] or 0,
        )
        if longest["duration_in_days"]:
            path.append(f"{longest['name']} ({longest['duration_in_days']})")
    rounds = context.description["call"]["rounds"]
    targets = [
        (index, r["time_to_decision_days"], r["time_to_decision_source"])
        for index, r in enumerate(rounds)
        if r["time_to_decision_days"] is not None
    ]
    target = context.description["call"]["target_decision_days"]
    if not rounds and target is not None:
        targets = [(None, target, "target_decision_days")]
    findings = []
    for index, limit, source in targets:
        if days <= limit:
            continue
        where = _("round %s") % (index + 1) if index is not None else _("the call")
        findings.append(
            context.finding(
                decision["key"],
                "duration_in_days",
                _(
                    "The steps up to the decision take %(days)s days, and %(where)s "
                    "allows %(limit)s from cut-off to decision: %(path)s."
                )
                % {
                    "days": days,
                    "where": where,
                    "limit": limit,
                    "path": " → ".join(path),
                },
                [_ref("call", source)]
                + [
                    _ref("step", "duration_in_days", k)
                    for k in _path_keys(context, decision)
                ],
            )
        )
    return findings


def _path_keys(context, decision):
    keys = []
    for stage in context.description["schedule"]["stages"]:
        if stage["stage"] > decision["stage"]:
            break
        longest = max(
            (context.by_key[k] for k in stage["steps"]),
            key=lambda s: s["duration_in_days"] or 0,
        )
        if longest["duration_in_days"]:
            keys.append(longest["key"])
    return keys


@rule("step_without_duration", WARNING, gettext_lazy("No duration"))
def step_without_duration(context):
    return [
        context.finding(
            s["key"],
            "duration_in_days",
            _("The step has no duration: it has no deadline and never lapses."),
            [_ref("step", "duration_in_days", s["key"])],
        )
        for s in context.enabled
        if s["kind"] != StepKinds.APPLICANT_ACTION
        and not s["decides_automatically"]
        and not s["duration_in_days"]
    ]


@rule("open_round_without_target", INFO, gettext_lazy("Open round without a target"))
def open_round_without_target(context):
    call = context.description["call"]
    if call["target_decision_days"] is not None:
        return []
    if any(r["open_ended"] for r in call["rounds"]):
        return [
            context.finding(
                None,
                "target_decision_days",
                _(
                    "A round has no cut-off, so its time to decision is unknown: "
                    "set the call's target days to decision to check the steps' "
                    "durations against it."
                ),
                [_ref("call", "target_decision_days")],
            )
        ]


# -- outcomes and what reads them ------------------------------------------------


@rule(
    "automatic_decision_without_inputs",
    WARNING,
    gettext_lazy("Automatic decision without inputs"),
)
def automatic_decision_without_inputs(context):
    """The automatic decision is taken on a proposal that passed a step that
    could have rejected it. When every such step runs only on some proposals,
    the others reach the decision with nothing to pass it on, and it is left
    to the call managers."""
    for step in context.enabled:
        if not (step["is_allocation_decision"] and step["decides_automatically"]):
            continue
        if _relies_on_provider_fulfilment(step):
            # Taken without an earlier step: the provider fulfils each order.
            continue
        gates = [
            s
            for s in context.enabled
            if (s["order"] or 0) < (step["order"] or 0) and context.ends_negatively(s)
        ]
        if gates and all(g["run_condition"] for g in gates):
            return [
                context.finding(
                    step["key"],
                    "decides_automatically",
                    _(
                        "Every step before the automatic decision that could "
                        "reject a proposal runs only on some proposals; for the "
                        "others the decision is left to the call managers."
                    ),
                    [_ref("step", "decides_automatically", step["key"])]
                    + [_ref("step", "run_condition", g["key"]) for g in gates],
                )
            ]
    return []


@rule(
    "negative_continues_on_gate",
    WARNING,
    gettext_lazy("Negative outcome changes nothing"),
)
def negative_continues_on_gate(context):
    findings = []
    for step in context.enabled:
        if step["on_negative"] != OnNegativeOutcomes.CONTINUE:
            continue
        key = step["key"]
        read = any(
            key in later["input_steps"]
            or (later["run_condition"] or {}).get("step") == key
            for later in context.enabled
            if later["stage"] > step["stage"]
        )
        if step["min_score_threshold"] is not None:
            message = _(
                "The step has a score threshold, and a proposal below it goes on "
                "to the next step anyway."
            )
        elif not read:
            message = _(
                "A negative outcome lets the proposal go on, and no later step "
                "reads this step or runs on its outcome: the outcome changes "
                "nothing."
            )
        else:
            continue
        findings.append(
            context.finding(
                key, "on_negative", message, [_ref("step", "on_negative", key)]
            )
        )
    return findings


@rule(
    "blind_review_identity_exposed",
    WARNING,
    gettext_lazy("Blind review exposes reviewers"),
)
def blind_review_identity_exposed(context):
    if not context.description["call"]["settings"].get(
        "reviewer_identity_visible_to_submitters"
    ):
        return []
    return [
        context.finding(
            s["key"],
            "blind_review",
            _(
                "The review is blind, and its report is released to the "
                "applicant while the call shows applicants who reviewed them."
            ),
            [
                _ref("step", "blind_review", s["key"]),
                _ref("step", "release_to_applicant", s["key"]),
                _ref("call", "reviewer_identity_visible_to_submitters"),
            ],
        )
        for s in context.enabled
        if s["blind_review"] and s["release_to_applicant"]
    ]


@rule("empty_reviewer_pool", WARNING, gettext_lazy("No reviewers"), live=True)
def empty_reviewer_pool(context):
    uses = [
        s
        for s in context.enabled
        if s["contributors"]["source"] == ContributorSources.ASSIGNED_REVIEWERS
    ]
    if not uses or context.holders(wd.REVIEWER):
        return []
    if models.CallReviewerPool.objects.filter(call=context.call).exists():
        return []
    return [
        context.finding(
            s["key"],
            "contributor_source",
            _(
                "The step's contributors are the assigned reviewers, and the "
                "call has no reviewers and an empty reviewer pool."
            ),
            [
                _ref("step", "contributor_source", s["key"]),
                _ref("call", CALL_REVIEWER_POOL),
            ],
        )
        for s in uses
    ]


# -- minimums a proposal cannot meet ----------------------------------------------


def _reads_proposal_reviews(step):
    """Whether the step's gates count the proposal's reviews rather than
    work of its own (``workflow_service._gate_work``)."""
    return step["contributors"]["source"] == ContributorSources.NONE


def _review_steps_before(context, step):
    """Earlier enabled steps whose assigned reviewers write the proposal's
    reviews: the only work a step without contributors counts."""
    return [
        s
        for s in context.earlier(step)
        if s["contributors"]["source"] == ContributorSources.ASSIGNED_REVIEWERS
    ]


@rule(
    "applicant_completes_evaluation",
    ERROR,
    gettext_lazy("Applicant evaluates own proposal"),
)
def applicant_completes_evaluation(context):
    return [
        context.finding(
            step["key"],
            "completer",
            _(
                "The applicant completes this step, and it is not an applicant "
                "action: they would assess or decide on their own proposal."
            ),
            [_ref("step", "completer", step["key"])],
        )
        for step in context.enabled
        if step["completer"] == ResponsibleRoles.APPLICANT
        and step["kind"] != StepKinds.APPLICANT_ACTION
    ]


@rule("minimum_without_reviews", WARNING, gettext_lazy("Not enough reviews collected"))
def minimum_without_reviews(context):
    """A step without contributors that needs reviews counts the
    proposal's: with no review step before it, only reviews a call manager
    adds by hand can ever complete it."""
    findings = []
    for step in context.enabled:
        minimum = step["contributors"]["min"]
        if not minimum or not _reads_proposal_reviews(step):
            continue
        review_steps = _review_steps_before(context, step)
        if not review_steps:
            findings.append(
                context.finding(
                    step["key"],
                    "min_contributions",
                    _(
                        "The step needs %(min)s reviews of the proposal, and no "
                        "earlier step collects reviews from assigned reviewers: "
                        "it completes only once a call manager adds them by hand."
                    )
                    % {"min": minimum},
                    [_ref("step", "min_contributions", step["key"])],
                )
            )
            continue
        minimums = [s["contributors"]["min"] for s in review_steps]
        if all(minimums) and minimum > sum(minimums):
            findings.append(
                context.finding(
                    step["key"],
                    "min_contributions",
                    _(
                        "The step needs %(min)s reviews of the proposal, and the "
                        "review steps before it ask for %(asked)s: a proposal "
                        "with only those waits for more reviewers."
                    )
                    % {"min": minimum, "asked": sum(minimums)},
                    [
                        _ref("step", "min_contributions", step["key"]),
                        *(
                            _ref("step", "min_contributions", s["key"])
                            for s in review_steps
                        ),
                    ],
                )
            )
    return findings


@rule("per_offering_minimum", WARNING, gettext_lazy("Per-offering minimum unreachable"))
def per_offering_minimum(context):
    """A step assessed per requested offering has one slot per offering a
    proposal requests: a minimum above that number is never met."""
    findings = []
    for step in context.enabled:
        contributors = step["contributors"]
        minimum = contributors["min"]
        if not contributors["per_requested_offering"] or not minimum or minimum < 2:
            continue
        because = [_ref("step", "min_contributions", step["key"])]
        offerings = context.live.requested_offerings if context.live else None
        if offerings is not None and minimum > offerings:
            findings.append(
                context.finding(
                    step["key"],
                    "min_contributions",
                    _(
                        "The step needs %(min)s contributions, one per requested "
                        "offering, and the call offers %(offerings)s: no "
                        "proposal can complete it."
                    )
                    % {"min": minimum, "offerings": offerings},
                    [*because, _ref("call", CALL_OFFERINGS)],
                    severity=ERROR,
                )
            )
            continue
        findings.append(
            context.finding(
                step["key"],
                "min_contributions",
                _(
                    "The step needs %(min)s contributions, one per requested "
                    "offering: a proposal requesting fewer offerings cannot "
                    "complete it."
                )
                % {"min": minimum},
                because,
            )
        )
    return findings


@rule(
    "score_gate_without_scores", ERROR, gettext_lazy("Score threshold with no scores")
)
def score_gate_without_scores(context):
    """A score threshold on a step without contributors reads its input
    steps' scores or the proposal's reviews (``workflow_service.gate_score``):
    with neither, every proposal scores nothing and stops there."""
    findings = []
    for step in context.enabled:
        if step["min_score_threshold"] is None or not _reads_proposal_reviews(step):
            continue
        scored_inputs = [
            key
            for key in step["input_steps"]
            if (source := context.by_key.get(key))
            and source["enabled"]
            and source["contributors"]["source"] != ContributorSources.NONE
        ]
        if scored_inputs or _review_steps_before(context, step):
            continue
        findings.append(
            context.finding(
                step["key"],
                "min_score_threshold",
                _(
                    "The step needs a score of %(threshold)s, and nothing before "
                    "it scores the proposal: no enabled input step has "
                    "contributors, and no earlier step collects reviews. Every "
                    "proposal would stop here."
                )
                % {"threshold": step["min_score_threshold"]},
                [
                    _ref("step", "min_score_threshold", step["key"]),
                    _ref("step", "input_steps", step["key"]),
                ],
            )
        )
    return findings


# -- steps run once per round -------------------------------------------------------

ROUND_SCOPED = "round_scoped"


def _round_scoped(context, step):
    return bool(context.raw[step["key"]].get(ROUND_SCOPED))


@rule("per_round_on_open_round", ERROR, gettext_lazy("Round step on an open round"))
def per_round_on_open_round(context):
    rounds = context.description["call"]["rounds"]
    if not any(r["open_ended"] for r in rounds):
        return []
    return [
        context.finding(
            s["key"],
            ROUND_SCOPED,
            _(
                "The step runs once per round, after its cut-off, and a round "
                "of the call has no cut-off."
            ),
            [_ref("step", ROUND_SCOPED, s["key"]), _ref("call", CALL_ROUNDS)],
        )
        for s in context.enabled
        if _round_scoped(context, s)
    ]


# Info, not a warning: a step run once per round after per-proposal steps is
# how a panel meets on a whole round's reviews, and a call with such a step
# already starts every proposal at the cut-off (round_step_not_at_cutoff).
# Waiting for the slowest proposal is the design; it is said, not flagged.
@rule(
    "per_round_after_per_proposal",
    INFO,
    gettext_lazy("Round waits for its slowest proposal"),
)
def per_round_after_per_proposal(context):
    findings = []
    seen_per_proposal = None
    for step in context.enabled:
        if _round_scoped(context, step):
            if seen_per_proposal is not None:
                findings.append(
                    context.finding(
                        step["key"],
                        ROUND_SCOPED,
                        _(
                            "The step runs once per round, after step '%s', which "
                            "runs per proposal: the round waits for its slowest "
                            "proposal."
                        )
                        % seen_per_proposal["name"],
                        [_ref("step", ROUND_SCOPED, step["key"])],
                    )
                )
        elif step["kind"] != StepKinds.APPLICANT_ACTION:
            seen_per_proposal = step
    return findings


@rule(
    "round_step_not_at_cutoff", ERROR, gettext_lazy("Evaluation not started at cut-off")
)
def round_step_not_at_cutoff(context):
    start = context.description["call"]["settings"].get("evaluation_start")
    if start == EvaluationStart.AT_CUTOFF:
        return []
    return [
        context.finding(
            s["key"],
            ROUND_SCOPED,
            _(
                "The step runs once per round, and the call starts each "
                "proposal's evaluation on submission, not at the round's "
                "cut-off: set the call to evaluate at the cut-off."
            ),
            [_ref("step", ROUND_SCOPED, s["key"]), _ref("call", "evaluation_start")],
        )
        for s in context.enabled
        if _round_scoped(context, s)
    ]


# What a step run once per round cannot be: its work is one report per
# contributor for the whole round, and it completes for the round.
ROUND_STEP_FORBIDDEN = (
    ("is_allocation_decision", "the allocation decision"),
    ("per_requested_offering", "per requested offering"),
    ("min_contributions", "a minimum of contributions"),
    ("max_contributions", "a maximum of contributions"),
    ("min_score_threshold", "a score threshold"),
    ("delegable_to", "delegation"),
    ("collects_reviewer_suggestions", "reviewer suggestions"),
    ("allows_change_requests", "requests for changes"),
)


# Who may write a round step's reports: nobody, the holders of an evaluator
# role, or of a call role.
ROUND_STEP_SOURCES = (
    ContributorSources.NONE,
    ContributorSources.PROPOSAL_ROLE,
    ContributorSources.CALL_ROLE,
)


@rule(
    "round_step_forbidden_setting",
    ERROR,
    gettext_lazy("Setting not allowed on a round step"),
)
def round_step_forbidden_setting(context):
    findings = []
    for step in context.enabled:
        if not _round_scoped(context, step):
            continue
        key = step["key"]
        because = [_ref("step", ROUND_SCOPED, key)]
        if step["kind"] not in (StepKinds.ASSESSMENT, StepKinds.DECISION):
            findings.append(
                context.finding(
                    key,
                    ROUND_SCOPED,
                    _("Only an assessment or a decision step runs once per round."),
                    [*because, _ref("step", "kind", key)],
                )
            )
        if step["contributors"]["source"] not in (None, *ROUND_STEP_SOURCES):
            findings.append(
                context.finding(
                    key,
                    "contributor_source",
                    _(
                        "A round step's reports are written by the holders of an "
                        "evaluator role or of a call role, or by nobody."
                    ),
                    [*because, _ref("step", "contributor_source", key)],
                )
            )
        if step["phase"] != WorkflowPhases.EVALUATION:
            findings.append(
                context.finding(
                    key,
                    ROUND_SCOPED,
                    _(
                        "A step after the award runs on one accepted proposal, "
                        "never once per round."
                    ),
                    [*because, _ref("step", "phase", key)],
                )
            )
        raw = context.raw[step["key"]]
        for field, what in ROUND_STEP_FORBIDDEN:
            if raw.get(field) not in (None, False, "", 0):
                findings.append(
                    context.finding(
                        step["key"],
                        field,
                        _("A step run once per round cannot have %s.") % what,
                        [
                            _ref("step", ROUND_SCOPED, step["key"]),
                            _ref("step", field, step["key"]),
                        ],
                    )
                )
    return findings


@rule(
    "round_assessment_without_contributors",
    WARNING,
    gettext_lazy("Round assessment without contributors"),
)
def round_assessment_without_contributors(context):
    return [
        context.finding(
            s["key"],
            "contributor_source",
            _(
                "The round assessment has no contributors to write round "
                "reports: it is completed only through the round's action."
            ),
            [_ref("step", "contributor_source", s["key"])],
        )
        for s in context.enabled
        if _round_scoped(context, s)
        and s["kind"] == StepKinds.ASSESSMENT
        and s["contributors"]["source"] in (None, ContributorSources.NONE)
    ]


@rule("round_advance_missing", INFO, gettext_lazy("No round-wide advance"))
def round_advance_missing(context):
    findings = []
    for step in context.enabled:
        if (
            step["kind"] != StepKinds.ASSESSMENT
            or step["outcome_set"]
            not in (OutcomeSets.FEASIBILITY, OutcomeSets.PER_OFFERING)
            or step["transition_mode"] != TransitionModes.MANUAL
            or _round_scoped(context, step)
        ):
            continue
        if any(
            _round_scoped(context, later)
            for later in context.enabled
            if later["stage"] > step["stage"]
        ):
            continue
        findings.append(
            context.finding(
                step["key"],
                "transition_mode",
                _(
                    "The technical assessment waits for a manual advance on each "
                    "proposal, and no later step runs per round to advance a "
                    "round's proposals together."
                ),
                [_ref("step", "transition_mode", step["key"])],
            )
        )
    return findings


# -- domain panels ----------------------------------------------------------------


def _panels(context):
    return [p for p in (context.document.get("panels") or []) if isinstance(p, dict)]


def _panel_roles(context, eligibility=PANEL_ELIGIBILITIES):
    if isinstance(eligibility, str):
        eligibility = (eligibility,)
    return [r for r in context.describer.roles if r.get("eligible_from") in eligibility]


def _live_panels(context):
    """The call's panels with their chair, and member counts by key."""
    if context.call is None:
        return None
    panels = models.CallPanel.objects.filter(call=context.call)
    return list(panels.values("key", "name", "chair_id")), {
        row["key"]: row["count"]
        for row in panels.values("key").annotate(count=Count("members"))
    }


@rule("panel_role_without_panels", WARNING, gettext_lazy("Panel role without panels"))
def panel_role_without_panels(context):
    if _panels(context):
        return []
    return [
        context.finding(
            None,
            CALL_PANELS,
            _(
                "The evaluator role '%s' is held through the proposal's panel, "
                "and the call has no panels."
            )
            % (role.get("name") or role.get("key")),
            [_ref("call", "eligible_from", detail=role.get("key"))],
        )
        for role in _panel_roles(context)
    ]


@rule(
    "panel_chair_role_many_holders",
    WARNING,
    gettext_lazy("Panel chair role needs more holders"),
)
def panel_chair_role_many_holders(context):
    return [
        context.finding(
            None,
            CALL_PROPOSAL_ROLES,
            _(
                "The evaluator role '%s' is held by the chair of the proposal's "
                "panel, one person, and needs more holders."
            )
            % (role.get("name") or role.get("key")),
            [_ref("call", "min_holders", detail=role.get("key"))],
        )
        for role in _panel_roles(context, PANEL_CHAIR_ELIGIBILITY)
        if (role.get("min_holders") or 0) > 1
    ]


@rule("panel_without_chair", WARNING, gettext_lazy("Panel without a chair"), live=True)
def panel_without_chair(context):
    if not _panel_roles(context, PANEL_CHAIR_ELIGIBILITY):
        return []
    live = _live_panels(context)
    if live is None:
        return []
    panels, _counts = live
    return [
        context.finding(
            None,
            CALL_PANELS,
            _(
                "The panel '%s' has no chair, and an evaluator role is held by "
                "the chair of the proposal's panel."
            )
            % panel["name"],
            [_ref("call", "panels", detail=panel["key"])],
        )
        for panel in panels
        if panel["chair_id"] is None
    ]


@rule("panel_too_small", WARNING, gettext_lazy("Panel too small"), live=True)
def panel_too_small(context):
    roles = _panel_roles(context, PANEL_MEMBER_ELIGIBILITY)
    if not roles:
        return []
    live = _live_panels(context)
    if live is None:
        return []
    panels, counts = live
    needed = max((r.get("min_holders") or 0) for r in roles)
    return [
        context.finding(
            None,
            CALL_PANELS,
            _(
                "The panel '%(panel)s' has %(count)s members, and an evaluator "
                "role held by its members needs %(needed)s."
            )
            % {
                "panel": panel["name"],
                "count": counts.get(panel["key"], 0),
                "needed": needed,
            },
            [_ref("call", "panels", detail=panel["key"])],
        )
        for panel in panels
        if counts.get(panel["key"], 0) < needed
    ]


def _domain_refs(panels, field):
    """What the panels' ``field`` names, as ``code -> names``.

    An entry is ``{"code", "name"}``, or a bare code as older documents
    wrote it, which names no domain in particular (``None``).
    """
    refs = {}
    for panel in panels:
        for entry in panel.get(field) or []:
            if isinstance(entry, dict):
                code, name = entry.get("code"), entry.get("name")
            else:
                code, name = entry, None
            if code is None:
                continue
            refs.setdefault(str(code), set()).add(
                name.strip().casefold() if isinstance(name, str) and name else None
            )
    return refs


def _covers(refs, domain):
    """Whether ``refs`` reach ``domain``: by its code and, if given, its name.

    Import skips an entry whose name is not the one this portal gives the
    code, so such an entry routes nothing here.
    """
    names = refs.get(str(domain.code), set())
    return None in names or domain.name.strip().casefold() in names


# Info, not a warning: a call takes every domain, and one whose panels cover a
# few (a sub-panel committee, say) moves the others to a panel by hand by
# design. The note names those domains, once, for the call managers.
@rule("domain_without_panel", INFO, gettext_lazy("Science domain without a panel"))
def domain_without_panel(context):
    panels = _panels(context)
    if not panels:
        return []
    covered = _domain_refs(panels, "science_domains")
    sub_covered = _domain_refs(panels, "science_sub_domains")
    uncovered = []
    for domain in structure_models.ScienceDomain.objects.prefetch_related(
        "subdomains"
    ).order_by("code"):
        if _covers(covered, domain):
            continue
        subs = list(domain.subdomains.all())
        if subs and all(_covers(sub_covered, sub) for sub in subs):
            continue
        uncovered.append(domain)
    if not uncovered:
        return []
    return [
        context.finding(
            None,
            CALL_PANELS,
            _(
                "No panel covers these science domains, so their proposals are "
                "moved to a panel by hand: %s."
            )
            % ", ".join(domain.name for domain in uncovered),
            [_ref("call", "panels", detail=str(domain.code)) for domain in uncovered],
        )
    ]


# -- after the award: extensions and the final report -------------------------------


def _phase_steps(context, phase):
    return [s for s in context.enabled if s["phase"] == phase]


def _phase_structure(context, phase, label, needs_decision):
    """What keeps ``phase`` from running, as
    ``CallWorkflowStep.phase_structure_errors`` refuses it."""
    steps = _phase_steps(context, phase)
    if not steps:
        return []
    findings = []
    first, last = steps[0], steps[-1]
    if not (
        first["kind"] == StepKinds.APPLICANT_ACTION
        and first["outcome_set"] == OutcomeSets.SUBMITTED
    ):
        findings.append(
            context.finding(
                first["key"],
                "kind",
                _(
                    "The %(phase)s opens with an applicant step whose outcome is "
                    "a submission; '%(step)s' is not one."
                )
                % {"phase": label, "step": first["name"]},
                [_ref("step", "phase", first["key"])],
            )
        )
    if (needs_decision or len(steps) > 1) and not (
        last["kind"] == StepKinds.DECISION
        and last["outcome_set"] == OutcomeSets.APPROVAL
    ):
        findings.append(
            context.finding(
                last["key"],
                "outcome_set",
                _(
                    "The %(phase)s ends with a decision that approves or declines "
                    "it; '%(step)s' is not one."
                )
                % {"phase": label, "step": last["name"]},
                [_ref("step", "phase", last["key"])],
            )
        )
    for step in steps[1:]:
        if step["kind"] == StepKinds.APPLICANT_ACTION:
            findings.append(
                context.finding(
                    step["key"],
                    "kind",
                    _(
                        "The %(phase)s has one applicant step, its first; "
                        "'%(step)s' is another."
                    )
                    % {"phase": label, "step": step["name"]},
                    [_ref("step", "phase", step["key"])],
                )
            )
    return findings


@rule("extension_structure", ERROR, gettext_lazy("Invalid extension phase"))
def extension_structure(context):
    return _phase_structure(context, WorkflowPhases.EXTENSION, _("extension"), True)


@rule("closing_structure", ERROR, gettext_lazy("Invalid closing phase"))
def closing_structure(context):
    return _phase_structure(context, WorkflowPhases.CLOSING, _("closing"), False)


# What a step after the award cannot do: it runs on an accepted proposal,
# whose award is settled and which a run never cancels.
POST_AWARD_FORBIDDEN = (
    ("is_allocation_decision", "settle the award"),
    ("include_award_response", "ask for the award's acceptance"),
    ("decides_automatically", "decide automatically"),
)


@rule(
    "post_award_forbidden_setting",
    ERROR,
    gettext_lazy("Setting not allowed after the award"),
)
def post_award_forbidden_setting(context):
    findings = []
    for step in context.enabled:
        if step["phase"] == WorkflowPhases.EVALUATION:
            continue
        key = step["key"]
        because = [_ref("step", "phase", key)]
        for field, what in POST_AWARD_FORBIDDEN:
            if step[field]:
                findings.append(
                    context.finding(
                        key,
                        field,
                        _("A step after the award cannot %s.") % what,
                        [*because, _ref("step", field, key)],
                    )
                )
        if step["run_condition"] is not None:
            findings.append(
                context.finding(
                    key,
                    "run_condition",
                    _(
                        "A step after the award runs on every run of its phase; "
                        "it has no run condition."
                    ),
                    [*because, _ref("step", "run_condition", key)],
                )
            )
        if step["on_negative"] == OnNegativeOutcomes.CANCEL:
            findings.append(
                context.finding(
                    key,
                    "on_negative",
                    _(
                        "A negative outcome after the award closes the run, never "
                        "the accepted proposal: it cannot cancel."
                    ),
                    [*because, _ref("step", "on_negative", key)],
                )
            )
    return findings


@rule("input_from_other_phase", ERROR, gettext_lazy("Reads a step of another phase"))
def input_from_other_phase(context):
    findings = []
    for step in context.enabled:
        for input_key in step["input_steps"]:
            source = context.by_key.get(input_key)
            if source is None or source["phase"] in (
                WorkflowPhases.EVALUATION,
                step["phase"],
            ):
                continue
            findings.append(
                context.finding(
                    step["key"],
                    "input_steps",
                    _(
                        "Step '%(step)s' reads step '%(input)s' of the %(phase)s; "
                        "a step reads the evaluation and its own phase."
                    )
                    % {
                        "step": step["name"],
                        "input": source["name"],
                        "phase": source["phase"],
                    },
                    [_ref("step", "input_steps", step["key"], detail=input_key)],
                )
            )
    return findings


@rule("stage_spans_phases", ERROR, gettext_lazy("Stage spans phases"))
def stage_spans_phases(context):
    findings = []
    previous = None
    for step in context.steps:
        if (
            previous is not None
            and step["enabled"]
            and step["parallel_with_previous"]
            and step["phase"] != previous["phase"]
        ):
            findings.append(
                context.finding(
                    step["key"],
                    "parallel_with_previous",
                    _(
                        "Step '%(step)s' runs alongside '%(previous)s' of another "
                        "phase; a stage never spans phases."
                    )
                    % {"step": step["name"], "previous": previous["name"]},
                    [_ref("step", "parallel_with_previous", step["key"])],
                )
            )
        previous = step
    return findings


@rule("no_final_report", INFO, gettext_lazy("No final report"))
def no_final_report(context):
    if _phase_steps(context, WorkflowPhases.CLOSING):
        return []
    return [
        context.finding(
            None,
            CALL_WORKFLOW_STEPS,
            _(
                "The call has no closing steps: no final report is asked of "
                "accepted proposals."
            ),
        )
    ]


@rule(
    "extension_without_offset",
    INFO,
    gettext_lazy("Extensions open until the project ends"),
)
def extension_without_offset(context):
    steps = _phase_steps(context, WorkflowPhases.EXTENSION)
    if not steps or steps[0]["project_end_offset_days"] is not None:
        return []
    return [
        context.finding(
            steps[0]["key"],
            "project_end_offset_days",
            _(
                "An extension can be asked for until the project's end date: "
                "no days before it are set."
            ),
            [_ref("step", "project_end_offset_days", steps[0]["key"])],
        )
    ]


# -- resource budgets ------------------------------------------------------------


def _hard_budget(context):
    return (
        context.description["call"]["settings"].get("budget_enforcement")
        == BudgetEnforcement.HARD
    )


@rule(
    "automatic_decision_defers_on_budget",
    INFO,
    gettext_lazy("Over-budget awards left to managers"),
)
def automatic_decision_defers_on_budget(context):
    if not _hard_budget(context):
        return []
    return [
        context.finding(
            s["key"],
            "decides_automatically",
            _(
                "The call holds awards to its budgets: an award that no longer "
                "fits is left to the call managers instead of being decided "
                "automatically."
            ),
            [
                _ref("call", "budget_enforcement"),
                _ref("step", "decides_automatically", s["key"]),
            ],
        )
        for s in context.enabled
        if s["decides_automatically"]
    ]


@rule(
    "budget_round_uncovered", WARNING, gettext_lazy("Round without a pool"), live=True
)
def budget_round_uncovered(context):
    if not _hard_budget(context):
        return []
    pools = context.live.budgets
    if any(row["round"] is None for row in pools):
        return []
    covered = {row["round"] for row in pools}
    return [
        context.finding(
            None,
            CALL_BUDGETS,
            _(
                "The call allocates first come, first served until its resource "
                "pools run out, and no pool covers the round '%s'."
            )
            % (round_["name"] or index + 1),
            [_ref("call", CALL_BUDGETS), _ref("call", CALL_ROUNDS)],
        )
        for index, round_ in enumerate(context.live.rounds)
        if index not in covered
    ]


def _least_request(rule):
    """The least a proposal may request under ``rule``, or None."""
    if rule["is_fixed"]:
        return rule["default_amount"]
    return rule["min_per_proposal"]


@rule(
    "budget_pool_below_request",
    WARNING,
    gettext_lazy("Pool below the smallest request"),
    live=True,
)
def budget_pool_below_request(context):
    """A pool smaller than the least a proposal may request of it fits no
    award at all."""
    rules = context.live.component_rules
    findings = []
    for pool in context.live.budgets:
        least = _least_request(rules[pool["rule"]])
        if least is None or pool["offered_total"] >= least:
            continue
        scope = (
            _("the whole call")
            if pool["round"] is None
            else (context.live.rounds[pool["round"]]["name"] or pool["round"] + 1)
        )
        findings.append(
            context.finding(
                None,
                CALL_BUDGETS,
                _(
                    "The %(component)s pool for %(scope)s offers less than the "
                    "least a proposal may request of it: no award fits."
                )
                % {"component": pool["component_type"], "scope": scope},
                [_ref("call", CALL_BUDGETS, detail=pool["component_type"])],
            )
        )
    return findings


def _amount(value):
    """A pool's total as people write it: grouped, without a ``.00``."""
    if value == value.to_integral_value():
        return f"{value:,.0f}"
    return f"{value:,}"


@rule(
    "budget_round_above_call",
    WARNING,
    gettext_lazy("Round pool exceeds the call pool"),
    live=True,
)
def budget_round_above_call(context):
    """An award draws on its round's pool and on the call-wide one and must fit
    both, so a round can never award more than the whole call's pool of the
    component: the rest of a larger round pool is never reached. Round pools
    that together exceed the call's are the usual overbooking, not this."""
    whole_call = {
        pool["rule"]: pool["offered_total"]
        for pool in context.live.budgets
        if pool["round"] is None
    }
    findings = []
    for pool in context.live.budgets:
        cap = whole_call.get(pool["rule"])
        if pool["round"] is None or cap is None or pool["offered_total"] <= cap:
            continue
        findings.append(
            context.finding(
                None,
                CALL_BUDGETS,
                _(
                    "The %(component)s pool for round '%(round)s' offers "
                    "%(round_total)s, more than the whole call's %(call_total)s: "
                    "at most %(call_total)s of it can be awarded."
                )
                % {
                    "component": pool["component_type"],
                    "round": context.live.rounds[pool["round"]]["name"]
                    or pool["round"] + 1,
                    "round_total": _amount(pool["offered_total"]),
                    "call_total": _amount(cap),
                },
                [_ref("call", CALL_BUDGETS, detail=pool["component_type"])],
            )
        )
    return findings


# -- the template the call came from -------------------------------------------


def _deviation_message(step):
    if step["change"] == workflow_diff.ADDED:
        return _("The step was added to the template's workflow.")
    if step["change"] == workflow_diff.REMOVED:
        return _("The template's step '%s' was removed.") % step["name"]
    fields = [
        f["field"] for f in step["fields"] if f["kind"] == workflow_diff.BEHAVIOUR
    ]
    if fields:
        return _("The step differs from the template: %s.") % ", ".join(fields)
    return _("The step moved from its place in the template.")


@rule(
    "deviates_from_template", INFO, gettext_lazy("Differs from the template"), live=True
)
def deviates_from_template(context):
    call = context.call
    if call.template_id is None:
        return []
    baseline = workflow_versions.resolve(call, "template")
    if baseline is None:
        return []
    changes = workflow_diff.diff(baseline, workflow_versions.call_document(call))
    return [
        context.finding(
            s["key"] if s["key"] in context.by_key else None,
            None,
            _deviation_message(s),
            [_ref("step", f["field"], s["key"]) for f in s["fields"]],
        )
        for s in changes["steps"]
        if s["kind"] == workflow_diff.BEHAVIOUR
    ]
