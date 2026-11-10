"""One normalised reading of a call's evaluation workflow.

``describe`` takes a call-transfer document -- ``call_transfer.export_call``
of a call, or a ``CallTemplate.document`` -- and says what each step does and
who takes part in it: the step's behaviour, one cell per actor (sees, reads
its outputs, contributes, completes, is notified) with the fields that made
it so, the label the applicant's tracker shows, the step's notification rules
and the flags raised over them, and the planned schedule. Every validation
view (step summaries, role and notification matrices, and the checks built on
them) reads the workflow through it, so they cannot disagree.

The description names rules, never people. ``live`` adds what only a call
has -- its rounds and how many people hold each role -- as counts.

Fields of features that land later are added through ``STEP_EXTENSIONS`` and
``CALL_EXTENSIONS``: each is a function of the step (or the document) that
returns the fields it describes, merged under ``extra``. A field absent from
the document is left out, never guessed.
"""

import dataclasses
import datetime
import functools
from collections.abc import Callable

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count
from django.utils.dateparse import parse_datetime

from waldur_core.permissions import models as permissions_models
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import (
    call_transfer,
    coi_declarations,
    models,
    notification_overrides,
    notification_rules,
    proposal_roles,
    status_labels,
)
from waldur_mastermind.proposal.enums import (
    SCORE_SPREAD_ABOVE,
    STEP_KINDS_MAP,
    AutomaticDecisionGuards,
    BudgetEnforcement,
    ContributorSources,
    DelegationTargets,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    OutcomeSets,
    ProposalRoleEligibility,
    RequestedOfferingStates,
    ResponsibleRoles,
    StepKinds,
    WorkflowPhases,
    WorkflowStepOutcomes,
    parse_contributor_source,
    parse_delegation_target,
)

SCHEMA = 1

# Actors: the columns of every matrix, in this order.
APPLICANT = "applicant"
CALL_MANAGER = "call_manager"
PANEL_CHAIR = "panel_chair"
PANEL_MEMBER = "panel_member"
REVIEWER = "reviewer"
OFFERING_TEAM = "offering_team"
SYSTEM = "system"
ROLE_PREFIX = "proposal_role:"
ROLE_LEAD_PREFIX = "proposal_role_lead:"

ACTOR_LABELS = {
    APPLICANT: "Applicant",
    CALL_MANAGER: "Call manager",
    PANEL_CHAIR: "Panel chair or vice-chair",
    PANEL_MEMBER: "Panel member",
    REVIEWER: "Assigned reviewer",
    OFFERING_TEAM: "Offering team",
    SYSTEM: "System",
}

# Evaluator roles held through the proposal's domain panel: who holds them
# is known once the proposal has its panel.
ROLE_ELIGIBILITY_NOTES = {
    ProposalRoleEligibility.PANEL_CHAIR: ["held_by_chair_of_proposal_panel"],
    ProposalRoleEligibility.PANEL_MEMBER: ["held_by_members_of_proposal_panel"],
}

# What an actor does on every step, whatever the steps say.
ACTOR_NOTES = {
    CALL_MANAGER: ["completes_any_evaluator_step", "reads_everything"],
    REVIEWER: ["only_proposals_with_work"],
    OFFERING_TEAM: ["reads_only_own_steps"],
    SYSTEM: ["expires_steps_with_duration"],
}

# Applicant tracker labels, as the applicant's step list derives them.
LABEL_STEP_NAME = "step_name"
LABEL_CONFIGURED = "configured"
LABEL_AWAITING_RESPONSE = "awaiting_response"
LABEL_DECISION = "decision"
LABEL_INTERNAL_CHECK = "internal_check"
LABEL_TEXTS = {
    LABEL_AWAITING_RESPONSE: "Awaiting your response",
    LABEL_DECISION: "Decision",
    LABEL_INTERNAL_CHECK: "Internal check",
}

COMPLETER_ACTORS = {
    ResponsibleRoles.APPLICANT: APPLICANT,
    ResponsibleRoles.CALL_MANAGER: CALL_MANAGER,
    ResponsibleRoles.OFFERING_MANAGER: OFFERING_TEAM,
    ResponsibleRoles.REVIEWER: REVIEWER,
    ResponsibleRoles.PANEL_MEMBER: PANEL_MEMBER,
    ResponsibleRoles.PANEL_CHAIR: PANEL_CHAIR,
}

CALL_ROLE_ACTORS = {
    "call_manager": CALL_MANAGER,
    "reviewer": REVIEWER,
    "panel_member": PANEL_MEMBER,
    "panel_chair": PANEL_CHAIR,
}

RECIPIENT_ACTORS = {
    NotificationRuleRecipients.APPLICANT: APPLICANT,
    NotificationRuleRecipients.ASSIGNED_REVIEWERS: REVIEWER,
    NotificationRuleRecipients.CALL_MANAGERS: CALL_MANAGER,
    NotificationRuleRecipients.ALL_POOL_REVIEWERS: REVIEWER,
    NotificationRuleRecipients.PANEL_CHAIR: PANEL_CHAIR,
    NotificationRuleRecipients.OFFERING_TEAMS: OFFERING_TEAM,
}

# Mail the workflow sends whatever the call's notification rules say, by the
# task in ``tasks.py`` that sends it, with its audience as actor kinds.
ALWAYS_SENT = (
    ("notify_user_about_proposal_state_update", "proposal_decided", [APPLICANT]),
    (
        "notify_proposal_creator_about_cancelled_proposal",
        "proposal_cancelled",
        [APPLICANT],
    ),
    (
        "notify_proposal_creator_about_carried_over_draft",
        "draft_carried_over",
        [APPLICANT],
    ),
    (
        "notify_proposal_creator_on_submission_deadline_approaching",
        "submission_deadline_approaching",
        [APPLICANT],
    ),
    (
        "notify_call_managers_about_new_proposal_submission",
        "proposal_submitted",
        [CALL_MANAGER],
    ),
    (
        "notify_call_managers_about_withdrawn_proposal",
        "proposal_withdrawn",
        [CALL_MANAGER],
    ),
    ("notify_call_managers_about_new_review", "review_submitted", [CALL_MANAGER]),
    ("notify_call_managers_about_rejected_review", "review_declined", [CALL_MANAGER]),
    ("notify_manager_when_reviews_are_completed", "reviews_completed", [CALL_MANAGER]),
    ("notify_call_managers_about_round_adoption", "round_adopted", [CALL_MANAGER]),
    ("notify_manager_on_round_cutoff", "round_cutoff", [CALL_MANAGER]),
    ("notify_offering_request_decision", "offering_request_decided", [CALL_MANAGER]),
    (
        "notify_provider_about_offering_request",
        "offering_requested",
        [OFFERING_TEAM],
    ),
    ("notify_managers_of_expired_batches", "assignment_batch_expired", [CALL_MANAGER]),
    (
        "_notify_reviewer_pool_invitation_expired",
        "pool_invitation_expired",
        [CALL_MANAGER],
    ),
    ("notify_reviewer_about_assignment", "review_assigned", [REVIEWER]),
    (
        "notify_reviewer_on_review_deadline_approaching",
        "review_deadline_approaching",
        [REVIEWER],
    ),
    (
        "notify_reviewer_on_proposal_decision",
        "proposal_decided_for_reviewer",
        [REVIEWER],
    ),
    ("notify_reviewer_on_round_start", "round_started", [REVIEWER]),
    ("send_reviewer_invitation_email", "pool_invitation", [REVIEWER]),
    ("send_assignment_batch_invitation", "assignment_batch_sent", [REVIEWER]),
    ("send_assignment_expiry_reminders", "assignment_expiring", [REVIEWER]),
    ("notify_proposal_role_changed", "evaluator_role_changed", ["proposal_role"]),
    (
        "notify_contribution_delegated",
        "contribution_delegated",
        ["proposal_role", OFFERING_TEAM],
    ),
    ("notify_change_request", "change_request", [APPLICANT, "step_actor"]),
    ("notify_changes_submitted", "changes_submitted", ["step_actor"]),
    (
        "notify_call_managers_about_round_step",
        "round_step_opened",
        [CALL_MANAGER],
    ),
    ("notify_round_report_authors", "round_report_requested", ["step_actor"]),
    ("send_round_report_reminders", "round_report_due", ["step_actor"]),
    (
        "notify_offering_teams_on_round_start",
        "offering_round_opened",
        [OFFERING_TEAM],
    ),
    ("send_offering_team_digests", "offering_team_digest", [OFFERING_TEAM]),
    (
        "notify_call_offering_terms_changed",
        "call_offering_terms_changed",
        [OFFERING_TEAM],
    ),
    ("notify_award_provision_failed", "award_provision_failed", [CALL_MANAGER]),
    # Written by a call manager to the applicants of chosen proposals.
    ("notify_proposals", "call_manager_message", [APPLICANT]),
)

# Tasks sending only what the rules say, and those that only call others.
RULE_DRIVEN_TASKS = frozenset(
    {
        "notify_workflow_step_event",
        "send_workflow_step_deadline_reminders",
        "_send_step_event",
    }
)
DISPATCHING_TASKS = frozenset(
    {"notify_proposal_decision", "notify_contribution_decision"}
)

SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


@dataclasses.dataclass
class LiveContext:
    """What only a call has, as counts: never a name or an address."""

    state: str | None = None
    rounds: list = dataclasses.field(default_factory=list)
    holders: dict = dataclasses.field(default_factory=dict)
    requested_offerings: int = 0
    offerings_without_managers: int = 0
    # Per step key, the step in each round: due date and opening.
    round_steps: dict = dataclasses.field(default_factory=dict)
    # Per panel key: whether it has a chair, and how many members.
    panels: dict = dataclasses.field(default_factory=dict)
    # What a proposal may request of each component of the call's offerings.
    component_rules: list = dataclasses.field(default_factory=list)
    # The call's resource pools: each names its rule by its place in
    # ``component_rules`` and its round by its place in ``rounds``, None for
    # the whole call.
    budgets: list = dataclasses.field(default_factory=list)


def live_context(call) -> LiveContext:
    """The live context of ``call``."""
    rounds = [
        {
            "uuid": r.uuid.hex,
            "name": r.name,
            "start_time": r.start_time,
            "cutoff_time": r.cutoff_time,
            "allocation_date": r.allocation_date,
            "review_duration_in_days": r.review_duration_in_days,
        }
        for r in call.round_set.order_by("start_time", "id")
    ]
    holders = {
        CALL_MANAGER: get_users(call, role_name=RoleEnum.CALL_MANAGER).count(),
        PANEL_MEMBER: get_users(call, role_name=RoleEnum.CALL_PANEL_MEMBER).count(),
        REVIEWER: get_users(call, role_name=RoleEnum.CALL_REVIEWER).count(),
        PANEL_CHAIR: len({call.panel_chair_id, call.panel_vice_chair_id} - {None}),
    }
    offering_ids = list(
        call.requestedoffering_set.exclude(
            state=RequestedOfferingStates.CANCELED
        ).values_list("offering_id", flat=True)
    )
    managed = set(
        permissions_models.UserRole.objects.filter(
            is_active=True,
            role__name=RoleEnum.OFFERING_MANAGER,
            content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
            object_id__in=offering_ids,
        ).values_list("object_id", flat=True)
    )
    round_steps = {}
    for row in (
        models.RoundStep.objects.filter(round__call=call)
        .order_by("round__start_time", "round_id")
        .values("call_step__key", "round__uuid", "due_at", "opened_at")
    ):
        round_steps.setdefault(row["call_step__key"], []).append(
            {
                "round_uuid": row["round__uuid"].hex,
                "due_at": row["due_at"],
                "opened_at": row["opened_at"],
            }
        )
    panels = {
        row["key"]: {"has_chair": row["chair_id"] is not None, "members": row["count"]}
        for row in models.CallPanel.objects.filter(call=call)
        .values("key", "chair_id")
        .annotate(count=Count("members"))
    }
    round_index = {
        pk: index
        for index, pk in enumerate(
            call.round_set.order_by("start_time", "id").values_list("pk", flat=True)
        )
    }
    rules = list(
        models.CallOfferingComponent.objects.filter(requested_offering__call=call)
        .exclude(requested_offering__state=RequestedOfferingStates.CANCELED)
        .prefetch_related("pools")
        .order_by("requested_offering_id", "created", "id")
    )
    component_rules = [
        {
            "component_type": rule.component_type,
            "min_per_proposal": rule.min_per_proposal,
            "max_per_proposal": rule.max_per_proposal,
            "default_amount": rule.default_amount,
            "is_fixed": rule.is_fixed,
        }
        for rule in rules
    ]
    budgets = [
        {
            "rule": index,
            "component_type": rule.component_type,
            "round": None if pool.round_id is None else round_index[pool.round_id],
            "offered_total": pool.offered_total,
        }
        for index, rule in enumerate(rules)
        for pool in sorted(rule.pools.all(), key=lambda p: (p.created, p.pk))
    ]
    return LiveContext(
        state=call.state,
        round_steps=round_steps,
        panels=panels,
        component_rules=component_rules,
        budgets=budgets,
        rounds=rounds,
        holders=holders,
        requested_offerings=len(set(offering_ids)),
        offerings_without_managers=len(set(offering_ids) - managed),
    )


# -- defaults ------------------------------------------------------------------


def _defaults(model):
    return {
        f.name: f.get_default()
        for f in model._meta.concrete_fields
        if not f.is_relation and f.has_default()
    }


@functools.cache
def _model_defaults(kind):
    model = {
        "step": models.CallWorkflowStep,
        "rule": models.CallWorkflowStepNotificationRule,
        "call": models.Call,
        "role": models.CallProposalRole,
    }[kind]
    return _defaults(model)


def _with_defaults(values, kind):
    """``values`` with the model's defaults for what the document leaves out:
    a template omits fields at their defaults, an export carries them all."""
    defaults = _model_defaults(kind)
    merged = dict(defaults)
    merged.update(
        {k: v for k, v in (values or {}).items() if v is not None or k not in defaults}
    )
    return merged


# -- references ----------------------------------------------------------------


def ref(source, field, step=None, detail=None):
    """What made a cell true: a field of a step, of the call or of a rule,
    or something the engine always does (``source`` "engine")."""
    return {"source": source, "step": step, "field": field, "detail": detail}


def _cell():
    return {"value": False, "because": []}


def _set(cell, because):
    cell["value"] = True
    if because not in cell["because"]:
        cell["because"].append(because)


class _StepView:
    """A document step with the attributes the engine's configuration
    predicates read (``notification_rules.is_internal_step`` and
    ``is_applicant_recipient``)."""

    def __init__(self, step):
        self.completer = step.get("completer")
        self.is_allocation_decision = bool(step.get("is_allocation_decision"))
        self.kind = step.get("kind")
        self.kind_definition = STEP_KINDS_MAP.get(self.kind)
        self.applicant_visible = bool(step.get("applicant_visible"))
        self.phase = step.get("phase") or WorkflowPhases.EVALUATION


def _role_key(source):
    """The evaluator role key a ``proposal_role:<key>`` value names."""
    try:
        kind, parameter = parse_contributor_source(source)
    except ValueError:
        return None
    return parameter if kind == ContributorSources.PROPOSAL_ROLE else None


def is_review_step(step) -> bool:
    """Whether the step's outcome is peer-review content, which the call's
    reviews_visible_to_submitters governs on the applicant side."""
    if step.get("outcome_set") == OutcomeSets.REVIEWED:
        return True
    return (
        step.get("kind") == StepKinds.DECISION
        and step.get("outcome_set") == OutcomeSets.APPROVAL
        and not step.get("is_allocation_decision")
        and step.get("phase") not in WorkflowPhases.POST_AWARD
    )


def applicant_label(step):
    """``(kind, text)`` of what the applicant's tracker calls ``step`` while
    it is current: the label the call configured for it, where the
    applicant's labels reach them; else the step's own name when it is
    visible to them, otherwise the stand-in for its stage."""
    configured = status_labels.configured(step, status_labels.APPLICANT).get(
        status_labels.ACTIVE
    )
    if configured:
        return LABEL_CONFIGURED, configured
    if step.get("applicant_visible"):
        return LABEL_STEP_NAME, step.get("name") or step.get("key") or ""
    if (
        step.get("kind") == StepKinds.APPLICANT_ACTION
        and step.get("outcome_set") == OutcomeSets.ACCEPTANCE
    ):
        kind = LABEL_AWAITING_RESPONSE
    elif step.get("is_allocation_decision"):
        kind = LABEL_DECISION
    else:
        kind = LABEL_INTERNAL_CHECK
    return kind, LABEL_TEXTS[kind]


# -- describing ----------------------------------------------------------------


class _Describer:
    def __init__(self, document, live):
        self.document = document if isinstance(document, dict) else {}
        self.live = live
        self.call = _with_defaults(self.document.get("call") or {}, "call")
        self.roles = [
            _with_defaults(r, "role")
            for r in (self.document.get("proposal_roles") or [])
            if isinstance(r, dict)
        ]
        self.role_by_key = {r.get("key"): r for r in self.roles}
        raw = [
            s
            for s in (self.document.get("workflow_steps") or [])
            if isinstance(s, dict)
        ]
        self.steps = sorted(
            (self._normalise_step(s) for s in raw),
            key=lambda s: (s.get("order") if s.get("order") is not None else 0),
        )
        self.by_key = {s["key"]: s for s in self.steps}

    @staticmethod
    def _normalise_step(step):
        values = _with_defaults(step, "step")
        # In the order an export lists them, whatever order a template has.
        values["notification_rules"] = sorted(
            (
                _with_defaults(r, "rule")
                for r in (step.get("notification_rules") or [])
                if isinstance(r, dict)
            ),
            key=lambda r: (
                r.get("trigger") or "",
                r.get("recipient") or "",
                r.get("proposal_role") or "",
                r.get("days_before") if r.get("days_before") is not None else -1,
            ),
        )
        values["input_steps"] = list(step.get("input_steps") or [])
        values["assigns_roles"] = list(step.get("assigns_roles") or [])
        values.setdefault("key", step.get("key") or "")
        return values

    # actors -------------------------------------------------------------

    def actors(self):
        keys = [
            APPLICANT,
            CALL_MANAGER,
            PANEL_CHAIR,
            PANEL_MEMBER,
            REVIEWER,
            OFFERING_TEAM,
        ]
        actors = [
            {
                "key": key,
                "kind": key,
                "role_key": None,
                "label": ACTOR_LABELS[key],
                "notes": ACTOR_NOTES.get(key, []),
                "holders": self.live.holders.get(key) if self.live else None,
            }
            for key in keys
        ]
        for role in self.roles:
            key = role.get("key")
            actors.append(
                {
                    "key": f"{ROLE_PREFIX}{key}",
                    "kind": "proposal_role",
                    "role_key": key,
                    "label": role.get("name") or key,
                    "notes": ROLE_ELIGIBILITY_NOTES.get(role.get("eligible_from"), []),
                    "holders": None,
                }
            )
            if role.get("allows_lead"):
                actors.append(
                    {
                        "key": f"{ROLE_LEAD_PREFIX}{key}",
                        "kind": "proposal_role_lead",
                        "role_key": key,
                        "label": f"Lead {role.get('name') or key}",
                        "notes": [],
                        "holders": None,
                    }
                )
        actors.append(
            {
                "key": SYSTEM,
                "kind": SYSTEM,
                "role_key": None,
                "label": ACTOR_LABELS[SYSTEM],
                "notes": ACTOR_NOTES[SYSTEM],
                "holders": None,
            }
        )
        return actors

    def _role_actor(self, key, lead=False):
        if key is None:
            return None
        if lead and (self.role_by_key.get(key) or {}).get("allows_lead"):
            return f"{ROLE_LEAD_PREFIX}{key}"
        return f"{ROLE_PREFIX}{key}"

    # who does what -------------------------------------------------------

    def lead_only_completion(self, step):
        return proposal_roles.lead_only_completion(
            step.get("contributors_lead_only"),
            step.get("completer"),
            step.get("completer_proposal_role"),
            _role_key(step.get("contributor_source")),
        )

    def completer_actors(self, step):
        """``[(actor, because)]`` completing ``step``."""
        completer = step.get("completer")
        key = step["key"]
        if completer == ResponsibleRoles.PROPOSAL_ROLE:
            role = step.get("completer_proposal_role")
            if not role:
                return []
            lead = self.lead_only_completion(step)
            because = [
                ref("step", "completer", key),
                ref("step", "completer_proposal_role", key),
            ]
            if lead:
                because.append(ref("step", "contributors_lead_only", key))
            return [(self._role_actor(role, lead=lead), because)]
        actor = COMPLETER_ACTORS.get(completer)
        return [(actor, [ref("step", "completer", key)])] if actor else []

    def contributor_actors(self, step):
        """``[(actor, because)]`` contributing to ``step``: its contributor
        source, and whoever its slots may be handed to."""
        key = step["key"]
        result = []
        try:
            kind, parameter = parse_contributor_source(step.get("contributor_source"))
        except ValueError:
            kind, parameter = ContributorSources.NONE, None
        because = [ref("step", "contributor_source", key)]
        if kind == ContributorSources.REQUESTED_OFFERING_TEAM:
            result.append((OFFERING_TEAM, because))
        elif kind == ContributorSources.ASSIGNED_REVIEWERS:
            result.append((REVIEWER, because))
        elif kind == ContributorSources.PROPOSAL_ROLE:
            lead = bool(step.get("contributors_lead_only"))
            if lead:
                because = [*because, ref("step", "contributors_lead_only", key)]
            result.append((self._role_actor(parameter, lead=lead), because))
        elif kind == ContributorSources.CALL_ROLE:
            actor = CALL_ROLE_ACTORS.get(parameter)
            if actor:
                result.append((actor, because))
        if step.get("per_requested_offering") and result:
            result = [
                (actor, [*b, ref("step", "per_requested_offering", key)])
                for actor, b in result
            ]
        try:
            target, target_parameter = parse_delegation_target(step.get("delegable_to"))
        except ValueError:
            target, target_parameter = DelegationTargets.NONE, None
        delegated = [ref("step", "delegable_to", key)]
        if target == DelegationTargets.OFFERING_TEAM:
            result.append((OFFERING_TEAM, delegated))
        elif target == DelegationTargets.PROPOSAL_ROLE:
            role = _role_key(f"{ROLE_PREFIX}{target_parameter}")
            result.append((self._role_actor(role), delegated))
        return result

    def workers(self, step):
        """``[(actor, because)]`` holding the step's work: those reading what
        it reads. On an applicant step, the applicant."""
        if step.get("kind") == StepKinds.APPLICANT_ACTION:
            return [(APPLICANT, [ref("step", "kind", step["key"])])]
        result = self.completer_actors(step) + self.contributor_actors(step)
        if step.get("contributors_lead_only"):
            # Every holder of the role works on the step; the lead writes.
            role = _role_key(step.get("contributor_source"))
            if role:
                result.append(
                    (
                        self._role_actor(role),
                        [ref("step", "contributors_lead_only", step["key"])],
                    )
                )
        return result

    @functools.cached_property
    def customised(self) -> set:
        """The mails the call words itself, by their mail key."""
        return {
            notification_overrides.mail_key(
                entry.get("mail_type") or "", entry.get("variant") or ""
            )
            for entry in (self.document.get("notification_overrides") or [])
            if isinstance(entry, dict) and entry.get("enabled", True)
        }

    @staticmethod
    def mail_types_of(event) -> list[str]:
        """The mail keys a call may word for an always-sent mail."""
        return [
            notification_overrides.mail_key(mail_type, variant)
            for mail_type, variant in notification_overrides.ALWAYS_SENT_MAIL_TYPES.get(
                event, []
            )
        ]

    def rule_actors(self, step, rule):
        """Actors a notification rule addresses on ``step``."""
        recipient = rule.get("recipient")
        view = _StepView(step)
        if notification_rules.is_applicant_recipient(recipient, view):
            return [APPLICANT]
        if recipient == NotificationRuleRecipients.RESPONSIBLE_ROLE:
            return [actor for actor, _ in self.completer_actors(step)]
        if recipient == NotificationRuleRecipients.PROPOSAL_ROLE:
            return (
                [self._role_actor(rule.get("proposal_role"))]
                if rule.get("proposal_role")
                else []
            )
        if recipient == NotificationRuleRecipients.PROPOSAL_ROLE_LEAD:
            role = rule.get("proposal_role")
            if not role or not (self.role_by_key.get(role) or {}).get("allows_lead"):
                return []
            return [self._role_actor(role, lead=True)]
        actor = RECIPIENT_ACTORS.get(recipient)
        return [actor] if actor else []

    # steps ---------------------------------------------------------------

    def stages(self):
        """Stage index of each step key, as ``CallWorkflowStep.stages``
        groups them: disabled steps included."""
        result, index = {}, -1
        for position, step in enumerate(self.steps):
            if position == 0 or not step.get("parallel_with_previous"):
                index += 1
            result[step["key"]] = index
        return result

    def coi_declaration(self, step):
        """The conflict-of-interest declaration a step's evaluators confirm,
        and where its wording comes from; the call team can edit it at any
        time. None on a step asking for none."""
        if not step.get("requires_coi_confirmation"):
            return None
        text, source = coi_declarations.resolve(
            step.get("coi_declaration_text"), self.call.get("coi_declaration_text")
        )
        return {"text": text, "source": source, "editable": True}

    def describe_step(self, step, actors, stages):
        key = step["key"]
        cells = {
            a["key"]: {
                "actor": a["key"],
                "sees": _cell(),
                "reads_outputs": _cell(),
                "contributes": _cell(),
                "completes": _cell(),
                "notified": {"value": False, "triggers": [], "because": []},
            }
            for a in actors
        }

        def cell(actor, name):
            return cells.get(actor, {}).get(name)

        for actor, because in self.completer_actors(step):
            for b in because:
                if cell(actor, "completes") is not None:
                    _set(cell(actor, "completes"), b)
        if step.get("is_allocation_decision") and step.get("decides_automatically"):
            _set(cells[SYSTEM]["completes"], ref("step", "decides_automatically", key))
        for actor, because in self.contributor_actors(step):
            for b in because:
                if cell(actor, "contributes") is not None:
                    _set(cell(actor, "contributes"), b)

        # Who reads what the step produced.
        _set(cells[CALL_MANAGER]["reads_outputs"], ref("engine", "oversees_call"))
        for actor, _ in self.completer_actors(step):
            if cell(actor, "reads_outputs") is not None:
                _set(cell(actor, "reads_outputs"), ref("step", "completer", key))
        for actor, because in self.contributor_actors(step):
            target = cell(actor, "reads_outputs")
            if target is None:
                continue
            if step.get("blind_review"):
                # Each reads their own contribution only.
                if not target["value"]:
                    target["because"].append(ref("step", "blind_review", key))
                continue
            _set(target, because[0])
        for later in self.steps:
            if key not in later["input_steps"]:
                continue
            for actor, _ in self.workers(later):
                if cell(actor, "reads_outputs") is not None:
                    _set(
                        cell(actor, "reads_outputs"),
                        ref("step", "input_steps", later["key"]),
                    )
        applicant_reads = cells[APPLICANT]["reads_outputs"]
        if step.get("kind") == StepKinds.ASSESSMENT and step.get(
            "release_to_applicant"
        ):
            _set(applicant_reads, ref("step", "release_to_applicant", key))
        if step.get("is_allocation_decision"):
            _set(applicant_reads, ref("step", "is_allocation_decision", key))
        if (
            step.get("phase") in WorkflowPhases.POST_AWARD
            and step.get("kind") == StepKinds.DECISION
        ):
            # The team's own extension request or final report, decided.
            _set(applicant_reads, ref("step", "phase", key))
        if is_review_step(step):
            if self.call.get("reviews_visible_to_submitters"):
                _set(applicant_reads, ref("call", "reviews_visible_to_submitters"))
        elif step.get("applicant_visible"):
            _set(applicant_reads, ref("step", "applicant_visible", key))

        # Who sees the step on the proposal's step list.
        _set(cells[CALL_MANAGER]["sees"], ref("engine", "oversees_call"))
        if step.get("applicant_visible"):
            _set(cells[APPLICANT]["sees"], ref("step", "applicant_visible", key))
        if step.get("completer") == ResponsibleRoles.APPLICANT:
            _set(cells[APPLICANT]["sees"], ref("step", "completer", key))
        for actor in cells:
            if actor in (APPLICANT, CALL_MANAGER, SYSTEM):
                continue
            if actor == OFFERING_TEAM:
                if step.get("applicant_visible"):
                    _set(cells[actor]["sees"], ref("step", "applicant_visible", key))
                for name in ("completes", "contributes"):
                    if cells[actor][name]["value"]:
                        _set(cells[actor]["sees"], cells[actor][name]["because"][0])
                continue
            _set(cells[actor]["sees"], ref("engine", "evaluator_reads_step_list"))

        notifications = []
        for rule in step["notification_rules"]:
            addressed = [a for a in self.rule_actors(step, rule) if a in cells]
            variant = notification_overrides.step_event_variant(
                notification_rules.is_applicant_recipient(
                    rule.get("recipient"), _StepView(step)
                )
            )
            mail = notification_overrides.mail_key("workflow_step_event", variant)
            notifications.append(
                {
                    "mail_type": "workflow_step_event",
                    "variant": variant,
                    "customised": mail in self.customised,
                    "trigger": rule.get("trigger"),
                    "recipient": rule.get("recipient"),
                    "proposal_role": rule.get("proposal_role"),
                    "notified_proposal_roles": [
                        r.get("name") if isinstance(r, dict) else r
                        for r in (rule.get("notified_proposal_roles") or [])
                    ],
                    "days_before": rule.get("days_before"),
                    "enabled": bool(rule.get("is_enabled", True)),
                    "actors": addressed,
                }
            )
            if not rule.get("is_enabled", True):
                continue
            for actor in addressed:
                notified = cells[actor]["notified"]
                notified["value"] = True
                trigger = {
                    "trigger": rule.get("trigger"),
                    "days_before": rule.get("days_before"),
                }
                if trigger not in notified["triggers"]:
                    notified["triggers"].append(trigger)
                notified["because"].append(
                    ref("rule", "recipient", key, detail=rule.get("trigger"))
                )

        label_kind, label = applicant_label(step)
        try:
            source_kind, source_parameter = parse_contributor_source(
                step.get("contributor_source")
            )
        except ValueError:
            source_kind, source_parameter = step.get("contributor_source"), None
        try:
            delegation, delegation_parameter = parse_delegation_target(
                step.get("delegable_to")
            )
        except ValueError:
            delegation, delegation_parameter = step.get("delegable_to"), None
        outcomes = [
            {
                "value": outcome,
                "polarity": "negative"
                if outcome in WorkflowStepOutcomes.NEGATIVE_OUTCOMES
                else "positive",
            }
            for outcome in OutcomeSets.OUTCOMES.get(step.get("outcome_set"), ())
        ]
        condition = step.get("run_condition") or {}
        checklist = step.get("checklist")
        description = {
            "key": key,
            "name": step.get("name") or key,
            "description": step.get("description") or "",
            "kind": step.get("kind"),
            "order": step.get("order"),
            "stage": stages[key],
            "parallel_with_previous": bool(step.get("parallel_with_previous")),
            "enabled": bool(step.get("is_enabled", True)),
            "phase": step.get("phase") or WorkflowPhases.EVALUATION,
            "project_end_offset_days": step.get("project_end_offset_days")
            if step.get("phase") in WorkflowPhases.POST_AWARD
            else None,
            "outcome_set": step.get("outcome_set"),
            "outcomes": outcomes,
            "on_negative": step.get("on_negative"),
            "transition_mode": step.get("transition_mode"),
            "duration_in_days": step.get("duration_in_days"),
            "run_condition": {
                "step": condition.get("step"),
                "outcomes": list(condition.get("outcomes") or []),
                "score_spread_above": condition.get(SCORE_SPREAD_ABOVE),
            }
            if condition.get("step")
            else None,
            "input_steps": list(step["input_steps"]),
            "is_allocation_decision": bool(step.get("is_allocation_decision")),
            "decides_automatically": bool(
                step.get("is_allocation_decision") and step.get("decides_automatically")
            ),
            "automatic_decision_guard": (
                step.get("automatic_decision_guard")
                or AutomaticDecisionGuards.EARLIER_STEP
            )
            if step.get("is_allocation_decision") and step.get("decides_automatically")
            else None,
            "allocation_time": step.get("allocation_time")
            if step.get("is_allocation_decision")
            else None,
            "include_award_response": bool(step.get("include_award_response")),
            "allows_change_requests": bool(step.get("allows_change_requests")),
            "allows_conditional_acceptance": step.get(
                "allows_conditional_acceptance", True
            )
            is not False,
            "completer": step.get("completer"),
            "completer_proposal_role": step.get("completer_proposal_role"),
            "completion_lead_only": self.lead_only_completion(step),
            "assigns_roles": list(step["assigns_roles"]),
            "contributors": {
                "source": source_kind,
                "parameter": source_parameter,
                "per_requested_offering": bool(step.get("per_requested_offering")),
                "lead_only": bool(step.get("contributors_lead_only")),
                "min": step.get("min_contributions"),
                "max": step.get("max_contributions"),
            },
            "delegable_to": {
                "target": delegation,
                "proposal_role": delegation_parameter,
            }
            if delegation
            else None,
            "blind_review": bool(step.get("blind_review")),
            "requires_coi_confirmation": bool(step.get("requires_coi_confirmation")),
            "coi_declaration": self.coi_declaration(step),
            "release_to_applicant": bool(step.get("release_to_applicant")),
            "collects_reviewer_suggestions": bool(
                step.get("collects_reviewer_suggestions")
            ),
            "min_score_threshold": str(step["min_score_threshold"])
            if step.get("min_score_threshold") is not None
            else None,
            "checklist": {
                "name": checklist.get("name"),
                "required": bool(step.get("checklist_required", True)),
            }
            if isinstance(checklist, dict)
            else None,
            "criteria": len(step.get("criteria") or []),
            # As configured, every audience; the applicant's reach them only
            # as ``applicant.labels`` says, and the call team's and the
            # offering teams' fall back to the evaluators'.
            "status_labels": step.get("status_labels") or {},
            # The counts the labels show (``status_labels.PLACEHOLDERS``).
            "label_counts": _label_counts(step.get("status_labels")),
            "applicant": {
                "visible": bool(step.get("applicant_visible")),
                "label_kind": label_kind,
                "label": label,
                # Every label the applicant reads for the step, by state.
                "labels": status_labels.configured(step, status_labels.APPLICANT),
                "internal": notification_rules.is_internal_step(_StepView(step)),
            },
            "roles": list(cells.values()),
            "notifications": notifications,
            "extra": {},
        }
        for extension in STEP_EXTENSIONS:
            description["extra"].update(extension(step, self) or {})
        return description

    # flags ---------------------------------------------------------------

    def flags(self, described):
        flags = []

        def flag(
            code,
            kind,
            severity,
            step,
            message,
            trigger=None,
            recipient=None,
            because=(),
            days_before=None,
        ):
            flags.append(
                {
                    "code": code,
                    "kind": kind,
                    "severity": severity,
                    "step": step,
                    "trigger": trigger,
                    # The lead time of the rule flagged, for a deadline reminder.
                    "days_before": days_before,
                    "recipient": recipient,
                    "message": message,
                    "because": list(because),
                }
            )

        enabled_steps = [s for s in described if s["enabled"]]

        # Labels nobody reads: the applicant's, on a step kept from them.
        for step in enabled_steps:
            if (step["status_labels"] or {}).get(status_labels.APPLICANT) and not step[
                "applicant"
            ]["labels"]:
                flag(
                    "applicant_labels_on_hidden_step",
                    "unreachable",
                    SEVERITY_INFO,
                    step["key"],
                    "The step is hidden from the applicant, whose tracker "
                    "keeps calling it "
                    f"'{step['applicant']['label']}': its applicant labels are "
                    "never shown.",
                    because=[
                        ref("step", "status_labels", step["key"]),
                        ref("step", "applicant_visible", step["key"]),
                    ],
                )

        def enabled_rules(step):
            return [n for n in step["notifications"] if n["enabled"]]

        def tells(step, trigger, actor):
            return any(
                n["trigger"] == trigger and actor in n["actors"]
                for n in enabled_rules(step)
            )

        # The applicant opens an extension run by asking for it, so its first
        # step awaits nobody who does not already know.
        opens_extension = next(
            (s["key"] for s in enabled_steps if s["phase"] == WorkflowPhases.EXTENSION),
            None,
        )
        for step in enabled_steps:
            key = step["key"]
            # Missing.
            if step["is_allocation_decision"] and not tells(
                step, NotificationRuleTriggers.STEP_COMPLETED, APPLICANT
            ):
                flag(
                    "decision_not_announced_to_team",
                    "missing",
                    SEVERITY_INFO,
                    key,
                    "No rule tells the applicant team of the allocation decision: "
                    "only the proposal's creator gets the decision mail that is "
                    "always sent.",
                    NotificationRuleTriggers.STEP_COMPLETED,
                    NotificationRuleRecipients.APPLICANT,
                    [ref("step", "is_allocation_decision", key)],
                )
            if (
                step["kind"] == StepKinds.APPLICANT_ACTION
                and key != opens_extension
                and not tells(step, NotificationRuleTriggers.STEP_STARTED, APPLICANT)
            ):
                flag(
                    "applicant_step_not_announced",
                    "missing",
                    SEVERITY_WARNING,
                    key,
                    "Nothing tells the applicant this step awaits them.",
                    NotificationRuleTriggers.STEP_STARTED,
                    NotificationRuleRecipients.APPLICANT,
                    [ref("step", "kind", key)],
                )
            completers = [
                c["actor"]
                for c in step["roles"]
                if c["completes"]["value"] and c["actor"] != SYSTEM
            ]
            if (
                step["duration_in_days"]
                and completers
                and not step["decides_automatically"]
                and not any(
                    tells(step, NotificationRuleTriggers.DEADLINE_APPROACHING, actor)
                    for actor in completers
                )
            ):
                flag(
                    "no_deadline_reminder",
                    "missing",
                    SEVERITY_WARNING,
                    key,
                    "The step has a deadline, but nobody completing it is reminded "
                    "before it lapses.",
                    NotificationRuleTriggers.DEADLINE_APPROACHING,
                    NotificationRuleRecipients.RESPONSIBLE_ROLE,
                    [ref("step", "duration_in_days", key)],
                )
            # Duplicates: one event, one audience, two rules. A responsible-role
            # rule follows the step's completer and the others do not, so it
            # meets one of them only while the completer is that audience --
            # as every step's default rules do on a step its call managers
            # complete. That overlap is by design and costs no mail.
            seen = {}
            for n in enabled_rules(step):
                if not n["actors"]:
                    continue
                signature = (
                    n["trigger"],
                    n["days_before"],
                    tuple(sorted(n["actors"])),
                    n["recipient"] == NotificationRuleRecipients.RESPONSIBLE_ROLE,
                )
                if signature in seen:
                    flag(
                        "duplicate_rule",
                        "duplicate",
                        SEVERITY_INFO,
                        key,
                        "Two rules address the same people on the same event; they "
                        "get one mail, so one of the rules is redundant.",
                        n["trigger"],
                        n["recipient"],
                        [ref("rule", "recipient", key, detail=seen[signature])],
                        days_before=n["days_before"],
                    )
                else:
                    seen[signature] = n["recipient"]
            # Unreachable.
            if step["label_counts"] and not _collects_contributions(step):
                flag(
                    "label_counts_without_contributions",
                    "unreachable",
                    SEVERITY_INFO,
                    key,
                    "The step's labels show counts of its contributions, but "
                    "nobody contributes to it: the counts stay at nought.",
                    because=[
                        ref("step", "status_labels", key),
                        ref("step", "contributor_source", key),
                    ],
                )
            for n in enabled_rules(step):
                recipient = n["recipient"]
                role = n["proposal_role"]
                if (
                    n["trigger"] == NotificationRuleTriggers.CONTRIBUTION_SUBMITTED
                    and not _collects_contributions(step)
                ):
                    flag(
                        "contribution_rule_without_contributions",
                        "unreachable",
                        SEVERITY_WARNING,
                        key,
                        "The rule fires on each contribution submitted, but "
                        "nobody contributes to the step: it never fires.",
                        n["trigger"],
                        recipient,
                        [
                            ref("rule", "trigger", key),
                            ref("step", "contributor_source", key),
                        ],
                        days_before=n["days_before"],
                    )
                if (
                    recipient == NotificationRuleRecipients.PROPOSAL_ROLE_LEAD
                    and role
                    and not (self.role_by_key.get(role) or {}).get("allows_lead")
                ):
                    flag(
                        "role_without_lead",
                        "unreachable",
                        SEVERITY_WARNING,
                        key,
                        f"The rule tells the lead of '{role}', a role without a lead.",
                        n["trigger"],
                        recipient,
                        [ref("rule", "proposal_role", key)],
                        days_before=n["days_before"],
                    )
                if (
                    recipient in NotificationRuleRecipients.NAMING_PROPOSAL_ROLE
                    and role
                ):
                    assigned = any(
                        role in earlier["assigns_roles"]
                        for earlier in enabled_steps
                        if (earlier["order"] or 0) < (step["order"] or 0)
                        or (
                            earlier["stage"] == step["stage"]
                            and earlier["key"] != key
                            and n["trigger"] != NotificationRuleTriggers.STEP_STARTED
                        )
                    ) or (
                        role in step["assigns_roles"]
                        and n["trigger"] != NotificationRuleTriggers.STEP_STARTED
                    )
                    # The step's own workers: its contributors or completer are
                    # the role's holders, so the step cannot run before the call
                    # managers assign them (role_assigned_by_hand notes that),
                    # and once it runs the rule reaches them.
                    works_the_step = role in (
                        step["completer_proposal_role"],
                        step["contributors"]["parameter"]
                        if step["contributors"]["source"]
                        == ContributorSources.PROPOSAL_ROLE
                        else None,
                    )
                    if not assigned and not works_the_step:
                        flag(
                            "role_not_assigned_before",
                            "unreachable",
                            SEVERITY_WARNING,
                            key,
                            f"No earlier step assigns '{role}'; the rule reaches its "
                            "holders only where call managers assigned them by hand.",
                            n["trigger"],
                            recipient,
                            [ref("rule", "proposal_role", key)],
                            days_before=n["days_before"],
                        )
                if self.live is not None:
                    addresses_chair = PANEL_CHAIR in n["actors"]
                    if addresses_chair and not self.live.holders.get(PANEL_CHAIR):
                        flag(
                            "no_panel_chair",
                            "unreachable",
                            SEVERITY_WARNING,
                            key,
                            "The rule tells the panel chair, and the call has no "
                            "chair or vice-chair.",
                            n["trigger"],
                            recipient,
                            [ref("call", "panel_chair")],
                            days_before=n["days_before"],
                        )
                    if (
                        OFFERING_TEAM in n["actors"]
                        and self.live.offerings_without_managers
                    ):
                        flag(
                            "offering_without_managers",
                            "unreachable",
                            SEVERITY_WARNING,
                            key,
                            f"{self.live.offerings_without_managers} requested "
                            "offering(s) have no managers to tell.",
                            n["trigger"],
                            recipient,
                            [ref("call", "requested_offerings")],
                            days_before=n["days_before"],
                        )
                # Leak risk: the applicant's mail names the step.
                if (
                    APPLICANT in n["actors"]
                    and step["applicant"]["internal"]
                    and not step["applicant"]["visible"]
                ):
                    flag(
                        "internal_step_named_to_applicant",
                        "leak_risk",
                        SEVERITY_WARNING,
                        key,
                        "The mail to the applicant names this step, which their "
                        "tracker hides behind 'Internal check'.",
                        n["trigger"],
                        recipient,
                        [ref("step", "applicant_visible", key)],
                        days_before=n["days_before"],
                    )
        # Two mails for one transition. Each phase runs on its own -- an
        # extension whenever one is asked for, the final report once at the
        # end -- so a step completing in one never starts a step of another.
        stage_steps = {}
        for step in enabled_steps:
            stage_steps.setdefault(step["stage"], []).append(step)
        ordered = sorted(stage_steps)
        for index, stage in enumerate(ordered[:-1]):
            following = stage_steps[ordered[index + 1]]
            for step in stage_steps[stage]:
                if not tells(step, NotificationRuleTriggers.STEP_COMPLETED, APPLICANT):
                    continue
                for nxt in following:
                    if nxt["phase"] != step["phase"]:
                        continue
                    if tells(nxt, NotificationRuleTriggers.STEP_STARTED, APPLICANT):
                        flag(
                            "two_mails_one_transition",
                            "duplicate",
                            SEVERITY_WARNING,
                            nxt["key"],
                            f"The applicant is told '{step['name']}' completed and "
                            f"'{nxt['name']}' started: two mails for one move.",
                            NotificationRuleTriggers.STEP_STARTED,
                            NotificationRuleRecipients.APPLICANT,
                            [
                                ref(
                                    "rule",
                                    "recipient",
                                    step["key"],
                                    detail="step_completed",
                                )
                            ],
                        )
        return flags

    # schedule ------------------------------------------------------------

    def schedule(self, described):
        """Planned offsets in days from the start of evaluation.

        Stages run one after another and the steps of a stage together; a
        stage ends with its longest member, which is on the critical path.
        Each step is planned from its stage's start for its own duration; a
        step with a run condition is planned as if taken. The steps of an
        extension or closing run on an accepted proposal, after the award:
        each phase is planned on its own, from the start of its run, in
        ``post_award``.
        """
        evaluation = [s for s in described if s["phase"] == WorkflowPhases.EVALUATION]
        planned = self._plan(evaluation)
        planned["post_award"] = []
        for phase in (WorkflowPhases.EXTENSION, WorkflowPhases.CLOSING):
            steps = [s for s in described if s["phase"] == phase]
            if not any(s["enabled"] for s in steps):
                continue
            plan = self._plan(steps)
            first = next(s for s in steps if s["enabled"])
            planned["post_award"].append(
                {
                    "phase": phase,
                    "project_end_offset_days": first["project_end_offset_days"],
                    "stages": plan["stages"],
                    "steps": plan["steps"],
                    "total_days": plan["total_days"],
                    "steps_without_duration": plan["steps_without_duration"],
                }
            )
        return planned

    @staticmethod
    def _plan(described):
        stages = {}
        for step in described:
            stages.setdefault(step["stage"], []).append(step)
        day, result, critical, undated, planned = 0, [], [], [], []
        for index in sorted(stages):
            enabled = [s for s in stages[index] if s["enabled"]]
            if not enabled:
                continue
            for s in enabled:
                if not s["duration_in_days"]:
                    undated.append(s["key"])
            longest = max(enabled, key=lambda s: s["duration_in_days"] or 0)
            length = longest["duration_in_days"] or 0
            result.append(
                {
                    "stage": index,
                    "steps": [s["key"] for s in enabled],
                    "start_day": day,
                    "end_day": day + length,
                }
            )
            critical.append(longest["key"])
            for s in enabled:
                planned.append(
                    {
                        "key": s["key"],
                        "stage": index,
                        "start_day": day,
                        "end_day": day + (s["duration_in_days"] or 0),
                        "conditional": s["run_condition"] is not None,
                        "critical": s["key"] == longest["key"],
                    }
                )
            day += length
        return {
            "stages": result,
            "steps": planned,
            "total_days": day,
            "critical_path": critical,
            "steps_without_duration": undated,
        }

    # call ----------------------------------------------------------------

    def rounds(self):
        if self.live is not None:
            rounds = self.live.rounds
        else:
            rounds = [
                r for r in (self.document.get("rounds") or []) if isinstance(r, dict)
            ]
        target = self.call.get("target_decision_days")
        result = []
        for r in rounds:
            start, cutoff, allocation = (
                _as_datetime(r.get(name))
                for name in ("start_time", "cutoff_time", "allocation_date")
            )
            if cutoff and allocation:
                days, source = (allocation - cutoff).days, "allocation_date"
            elif target is not None:
                days, source = target, "target_decision_days"
            elif r.get("review_duration_in_days") is not None:
                days, source = (
                    r.get("review_duration_in_days"),
                    "review_duration_in_days",
                )
            else:
                days, source = None, None
            result.append(
                {
                    # Which of the call's rounds: a document's are unsaved.
                    "uuid": r.get("uuid") if self.live is not None else None,
                    "name": r.get("name"),
                    "start_time": start,
                    "cutoff_time": cutoff,
                    "allocation_date": allocation,
                    "open_ended": cutoff is None,
                    "review_duration_in_days": r.get("review_duration_in_days"),
                    "time_to_decision_days": days,
                    "time_to_decision_source": source,
                }
            )
        return result

    def describe(self):
        actors = self.actors()
        stages = self.stages()
        steps = [self.describe_step(s, actors, stages) for s in self.steps]
        call = self.call
        description = {
            "schema": SCHEMA,
            "call": {
                "name": call.get("name") or "",
                "state": self.live.state if self.live else None,
                "template": None,
                "settings": {
                    name: call.get(name)
                    for name in (
                        "reviews_visible_to_submitters",
                        "reviewer_identity_visible_to_submitters",
                        "evaluation_start",
                        "publish_results",
                        "adoption_required",
                        "undecided_at_round_completion",
                        "carry_over_drafts",
                        "coi_declaration_text",
                        "fixed_duration_in_days",
                        "allowed_duration_months",
                        "order_author",
                        "budget_enforcement",
                        "applicant_cc_roles",
                    )
                },
                "target_decision_days": call.get("target_decision_days"),
                "rounds": self.rounds(),
                "extra": {},
            },
            "actors": actors,
            "steps": steps,
            "notifications": {
                "flags": self.flags(steps),
                "always_sent": [
                    {
                        "task": task,
                        "event": event,
                        "audience": list(audience),
                        "mail_types": self.mail_types_of(event),
                        "customised": any(
                            key in self.customised for key in self.mail_types_of(event)
                        ),
                    }
                    for task, event, audience in ALWAYS_SENT
                ],
                "customised": sorted(self.customised),
            },
            "schedule": self.schedule(steps),
            "issues_structural": [],
        }
        # A document from before the wording existed reads as without one.
        settings = description["call"]["settings"]
        settings["coi_declaration_text"] = settings["coi_declaration_text"] or ""
        for extension in CALL_EXTENSIONS:
            description["call"]["extra"].update(extension(self.document, self) or {})
        return description


def _label_counts(labels) -> list[str]:
    """The placeholders a step's labels name, each once, sorted."""
    names = set()
    for entry in (labels or {}).values() if isinstance(labels, dict) else ():
        if not isinstance(entry, dict):
            continue
        texts = [entry.get(state) for state in status_labels.STATES]
        texts += list((entry.get(status_labels.OUTCOMES) or {}).values())
        for text in texts:
            names.update(status_labels.placeholders(text))
    return sorted(names)


def _collects_contributions(step) -> bool:
    contributors = step["contributors"]
    return (
        contributors["source"] not in (None, ContributorSources.NONE)
        or contributors["per_requested_offering"]
    )


def _as_datetime(value):
    if value is None or isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str):
        return parse_datetime(value)
    return None


# Functions ``(step document, describer) -> dict`` adding fields of later
# features to a step's ``extra``; ``(document, describer) -> dict`` for the
# call's.
def _drawn_step_fields(step, describer):
    """What the workflow diagram draws of step features a call may not have
    yet: a step run once per round, and the statuses each audience's
    dashboard shows. Left out where the document has no such field."""
    extra = {}
    if "round_scoped" in step:
        extra["round_scoped"] = bool(step["round_scoped"])
    if step.get("status_labels"):
        extra["status_labels"] = step["status_labels"]
    return extra


STEP_EXTENSIONS: list[Callable] = [_drawn_step_fields]
CALL_EXTENSIONS: list[Callable] = []


def _round_step_extension(step, describer):
    """A step run once per round: who writes its round reports, how it
    completes, and on a call its due date and opening in each round. Only
    for documents whose steps carry ``round_scoped``."""
    if "round_scoped" not in step:
        return {}
    scoped = bool(step.get("round_scoped"))
    result = {"round_scoped": scoped}
    if not scoped:
        return result
    result["round_report_writers"] = [
        actor for actor, _ in describer.contributor_actors(step)
    ]
    result["round_completion"] = (
        "reports"
        if step.get("kind") == StepKinds.ASSESSMENT
        and step.get("outcome_set") == OutcomeSets.DONE
        else "round_action"
    )
    if describer.live is not None:
        result["rounds"] = [
            {
                "round_uuid": r["round_uuid"],
                "due_at": r["due_at"].isoformat() if r["due_at"] else None,
                "opened": r["opened_at"] is not None,
            }
            for r in describer.live.round_steps.get(step.get("key"), [])
        ]
    return result


STEP_EXTENSIONS.append(_round_step_extension)


def _panel_step_extension(step, describer):
    """The evaluator roles of the step held through the proposal's domain
    panel -- assigned by it, completing it or contributing to it -- so a
    reader sees that who works on it depends on the proposal's panel."""
    keys = list(step.get("assigns_roles") or [])
    if step.get("completer") == ResponsibleRoles.PROPOSAL_ROLE:
        keys.append(step.get("completer_proposal_role"))
    keys.append(_role_key(step.get("contributor_source")))
    roles = [
        {"role": key, "eligible_from": describer.role_by_key[key]["eligible_from"]}
        for key in dict.fromkeys(k for k in keys if k)
        if (describer.role_by_key.get(key) or {}).get("eligible_from")
        in ProposalRoleEligibility.PANEL_BASED
    ]
    return {"panel_roles": roles} if roles else {}


STEP_EXTENSIONS.append(_panel_step_extension)


def _panels_call_extension(document, describer):
    """The call's domain panels: the research domains each routes, and on a
    call whether it has a chair and how many members -- counts only."""
    panels = []
    for panel in document.get("panels") or []:
        if not isinstance(panel, dict):
            continue
        entry = {
            "key": panel.get("key"),
            "name": panel.get("name"),
            "science_domains": len(panel.get("science_domains") or []),
            "science_sub_domains": len(panel.get("science_sub_domains") or []),
        }
        if describer.live is not None:
            staffed = describer.live.panels.get(panel.get("key")) or {}
            entry["has_chair"] = bool(staffed.get("has_chair"))
            entry["members"] = staffed.get("members", 0)
        panels.append(entry)
    roles = [
        role.get("key")
        for role in describer.roles
        if role.get("eligible_from") in ProposalRoleEligibility.PANEL_BASED
    ]
    return {"panels": panels, "panel_roles": roles}


def _budgets_call_extension(document, describer):
    """How the call holds its awards to its resource pools: enforcement,
    whether an award that does not fit is decided first come, first served
    (a hard budget), and on a call its component rules and pools."""
    enforcement = describer.call.get("budget_enforcement") or BudgetEnforcement.SOFT
    result = {
        "budget_enforcement": enforcement,
        "first_come_first_served": enforcement == BudgetEnforcement.HARD,
    }
    if describer.live is not None:
        result["component_rules"] = [
            {
                **row,
                **{
                    name: str(row[name]) if row[name] is not None else None
                    for name in (
                        "min_per_proposal",
                        "max_per_proposal",
                        "default_amount",
                    )
                },
            }
            for row in describer.live.component_rules
        ]
        result["budgets"] = [
            {**row, "offered_total": str(row["offered_total"])}
            for row in describer.live.budgets
        ]
    return result


def _phases_call_extension(document, describer):
    """The steps after the award: the extension the applicant team may ask
    for as often as needed, and the closing with the final report, each in
    order with the days before the project's end that time it."""
    phases = {}
    for phase in (WorkflowPhases.EXTENSION, WorkflowPhases.CLOSING):
        steps = [
            s
            for s in describer.steps
            if s.get("phase") == phase and s.get("is_enabled", True)
        ]
        if not steps:
            continue
        phases[phase] = {
            "steps": [s["key"] for s in steps],
            "project_end_offset_days": steps[0].get("project_end_offset_days"),
            "opens_with": steps[0]["key"],
            "ends_with": steps[-1]["key"],
        }
    return {"phases": phases}


CALL_EXTENSIONS.extend(
    [_panels_call_extension, _budgets_call_extension, _phases_call_extension]
)


def describe(document: dict, live: LiveContext | None = None) -> dict:
    """The description of the workflow ``document`` configures."""
    return _Describer(document, live).describe()


def describe_call(call) -> dict:
    """The description of ``call`` with its live counts."""
    document, _ = call_transfer.export_call(
        call, {"documents": False, "offerings": False}
    )
    description = describe(document, live_context(call))
    if call.template_id is not None:
        description["call"]["template"] = call.template.name
    return description
