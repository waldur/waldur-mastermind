import uuid

from .structures import DefaultCallStep, StepKindDefinition


class WorkflowStepInstanceStatuses:
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    EXPIRED = "expired"
    SKIPPED = "skipped"
    # Stopped while running beside a step of its stage whose outcome ended
    # the workflow: it never finished, and nobody works on it any more.
    WITHDRAWN = "withdrawn"

    CHOICES = (
        (PENDING, "Pending"),
        (ACTIVE, "Active"),
        (COMPLETED, "Completed"),
        (EXPIRED, "Expired"),
        (SKIPPED, "Skipped"),
        (WITHDRAWN, "Withdrawn"),
    )

    # A step that has ended, one way or another: its stage no longer waits
    # for it.
    ENDED = frozenset({COMPLETED, EXPIRED, SKIPPED, WITHDRAWN})


class CheckRunStatuses:
    """Whether checks or scenarios asked to run in the background are still
    on their way."""

    IDLE = "idle"
    RUNNING = "running"

    CHOICES = ((IDLE, IDLE), (RUNNING, RUNNING))


class WorkflowPhases:
    """When a call step runs in a proposal's life.

    ``evaluation`` steps run from submission to the award. The other two run
    on an accepted proposal, each in a run of its own (``ProposalRun``):
    ``extension`` whenever the applicant asks to extend the allocation,
    ``closing`` once, for the final report at the end of the allocation.
    """

    EVALUATION = "evaluation"
    EXTENSION = "extension"
    CLOSING = "closing"

    CHOICES = (
        (EVALUATION, "Evaluation"),
        (EXTENSION, "Extension"),
        (CLOSING, "Closing"),
    )

    # Phases that run on an accepted proposal, in runs.
    POST_AWARD = frozenset({EXTENSION, CLOSING})


class ProposalRunStates:
    """Where a post-award run of an accepted proposal stands."""

    OPEN = "open"
    # The extension was granted, or the final report accepted.
    APPROVED = "approved"
    # The extension was not granted, or the final report not accepted.
    REJECTED = "rejected"
    # The applicant let the request lapse, or it was called off.
    WITHDRAWN = "withdrawn"
    # The decision lapsed; nothing was granted.
    EXPIRED = "expired"

    CHOICES = (
        (OPEN, "Open"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
        (WITHDRAWN, "Withdrawn"),
        (EXPIRED, "Expired"),
    )


class PostAwardStatuses:
    """What an accepted proposal's dashboard says of its post-award work."""

    EXTENSION_REQUESTED = "extension_requested"
    EXTENSION_IN_ASSESSMENT = "extension_in_assessment"
    EXTENSION_PENDING_RESULTS = "extension_pending_results"
    PROJECT_EXTENDED = "project_extended"
    PENDING_FINAL_REPORT = "pending_final_report"
    FINAL_REPORT_SUBMITTED = "final_report_submitted"
    PROJECT_COMPLETED = "project_completed"

    CHOICES = (
        (EXTENSION_REQUESTED, "Extension request open"),
        (EXTENSION_IN_ASSESSMENT, "Pending extension request assessment"),
        (EXTENSION_PENDING_RESULTS, "Pending extension request results"),
        (PROJECT_EXTENDED, "Project extended"),
        (PENDING_FINAL_REPORT, "Pending final report"),
        (FINAL_REPORT_SUBMITTED, "Final report submitted"),
        (PROJECT_COMPLETED, "Project completed"),
    )


class ResponsibleRoles:
    CALL_MANAGER = "call_manager"
    OFFERING_MANAGER = "offering_manager"
    REVIEWER = "reviewer"
    PANEL_MEMBER = "panel_member"
    # The call's panel chair and vice-chair (Call.panel_chair and
    # Call.panel_vice_chair), while they sit on its panel.
    PANEL_CHAIR = "panel_chair"
    APPLICANT = "applicant"
    # The holders of one of the call's evaluator roles on the proposal; the
    # call step names which one (completer_proposal_role).
    PROPOSAL_ROLE = "proposal_role"

    CHOICES = (
        (CALL_MANAGER, "Call manager"),
        (OFFERING_MANAGER, "Offering manager"),
        (REVIEWER, "Reviewer"),
        (PANEL_MEMBER, "Panel member"),
        (PANEL_CHAIR, "Panel chair or vice-chair"),
        (APPLICANT, "Applicant"),
        (PROPOSAL_ROLE, "Evaluator role on the proposal"),
    )


class ProposalRoleEligibility:
    """Who may hold one of a call's evaluator roles on a proposal."""

    CALL_MANAGER = "call_role:manager"
    CALL_REVIEWER = "call_role:reviewer"
    CALL_PANEL_MEMBER = "call_role:panel_member"
    # Reviewers of the call who submitted a review of the proposal.
    PROPOSAL_REVIEWERS = "proposal_reviewers"
    ANY_USER = "any_user"
    # The chair, or the members, of the call panel the proposal is routed to.
    PANEL_CHAIR = "panel:chair"
    PANEL_MEMBER = "panel:member"

    # Who may hold the role is known only once the proposal has its panel.
    PANEL_BASED = (PANEL_CHAIR, PANEL_MEMBER)

    CHOICES = (
        (CALL_MANAGER, "Call managers of the call"),
        (CALL_REVIEWER, "Reviewers of the call"),
        (CALL_PANEL_MEMBER, "Panel members of the call"),
        (PROPOSAL_REVIEWERS, "Reviewers who submitted a review of the proposal"),
        (ANY_USER, "Any user"),
        (PANEL_CHAIR, "The chair of the proposal's panel"),
        (PANEL_MEMBER, "Members of the proposal's panel"),
    )


class NotificationRuleTriggers:
    """Workflow events a call's notification rule can react to."""

    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_REJECTED = "step_rejected"
    STEP_EXPIRED = "step_expired"
    DEADLINE_APPROACHING = "deadline_approaching"
    # The step's actor sent the proposal back to the applicant for changes.
    CHANGES_REQUESTED = "changes_requested"
    # The applicant submitted the changes asked of them.
    CHANGES_SUBMITTED = "changes_submitted"
    # A submitted contribution to the step was reopened for its contributor.
    CONTRIBUTION_REOPENED = "contribution_reopened"
    # One contribution to the step was submitted: a review, a rapporteur's
    # report, one offering's assessment. The mail never names who.
    CONTRIBUTION_SUBMITTED = "contribution_submitted"

    # Events that may happen on a step any number of times: each one is
    # announced, rather than the first alone.
    REPEATABLE = frozenset(
        {
            CHANGES_REQUESTED,
            CHANGES_SUBMITTED,
            CONTRIBUTION_REOPENED,
            CONTRIBUTION_SUBMITTED,
        }
    )

    CHOICES = (
        (STEP_STARTED, "Step started"),
        (STEP_COMPLETED, "Step completed"),
        (STEP_REJECTED, "Step rejected"),
        (STEP_EXPIRED, "Step expired"),
        (DEADLINE_APPROACHING, "Deadline approaching"),
        (CHANGES_REQUESTED, "Changes requested from the applicant"),
        (CHANGES_SUBMITTED, "Changes submitted by the applicant"),
        (CONTRIBUTION_REOPENED, "Contribution reopened"),
        (CONTRIBUTION_SUBMITTED, "Contribution submitted"),
    )


class ChangeRequestResolutions:
    """How a request for changes was closed."""

    # The applicant (or contributor) handed the changes in.
    SUBMITTED = "submitted"
    # Its due date passed without them; the step went back to its actor.
    LAPSED = "lapsed"
    # The proposal was withdrawn, or the contribution taken out of the work.
    WITHDRAWN = "withdrawn"
    # Called off by those overseeing the step, or ended with the step.
    CANCELLED = "cancelled"

    CHOICES = (
        (SUBMITTED, "Submitted"),
        (LAPSED, "Lapsed"),
        (WITHDRAWN, "Withdrawn"),
        (CANCELLED, "Cancelled"),
    )


class ChangeRequestParts:
    """The parts of a submitted proposal a request for changes can unlock.

    A Project details field is named ``field:<name>``; the rest by name.
    """

    FIELD_PREFIX = "field:"
    # The Project details fields the applicant edits through
    # update_project_details, besides those the call configures
    # (PROPOSAL_CONFIGURABLE_FIELDS).
    FIELDS = ("name", "project_summary", "description", "science_sub_domain")
    DURATION_FIELD = "requested_duration_months"
    DOCUMENTS = "documents"
    RESOURCES = "resources"
    TEAM = "team"
    COMPLIANCE = "compliance"

    OTHER_PARTS = (DOCUMENTS, RESOURCES, TEAM, COMPLIANCE)

    # On a post-award run: the answers of its applicant step -- the
    # extension request's form, the final report -- and what an extension
    # asks for, its end date and limits. The proposal itself stays locked.
    RUN_FORM = "run_form"
    RUN_REQUEST = "run_request"
    RUN_PARTS = (RUN_FORM, RUN_REQUEST)

    @classmethod
    def field(cls, name):
        return f"{cls.FIELD_PREFIX}{name}"


class NotificationRuleRecipients:
    """Who a call's notification rule addresses. Resolved per proposal at send time."""

    APPLICANT = "applicant"
    RESPONSIBLE_ROLE = "responsible_role"
    ASSIGNED_REVIEWERS = "assigned_reviewers"
    CALL_MANAGERS = "call_managers"
    ALL_POOL_REVIEWERS = "all_pool_reviewers"
    PANEL_CHAIR = "panel_chair"
    # The holders of the evaluator role the rule names (proposal_role).
    PROPOSAL_ROLE = "proposal_role"
    # The lead of the evaluator role the rule names (proposal_role).
    PROPOSAL_ROLE_LEAD = "proposal_role_lead"
    # Recipients naming an evaluator role.
    NAMING_PROPOSAL_ROLE = (PROPOSAL_ROLE, PROPOSAL_ROLE_LEAD)
    # The managers of the offerings the proposal involves: requested before
    # the allocation decision, awarded once it is approved.
    OFFERING_TEAMS = "offering_teams"

    CHOICES = (
        (APPLICANT, "Applicant"),
        (RESPONSIBLE_ROLE, "Responsible role"),
        (ASSIGNED_REVIEWERS, "Assigned reviewers"),
        (CALL_MANAGERS, "Call managers"),
        (ALL_POOL_REVIEWERS, "All pool reviewers"),
        (PANEL_CHAIR, "Panel chair and vice-chair"),
        (PROPOSAL_ROLE, "Holders of an evaluator role"),
        (PROPOSAL_ROLE_LEAD, "Lead of an evaluator role"),
        (OFFERING_TEAMS, "Teams of the proposal's offerings"),
    )


class TransitionModes:
    AUTOMATIC_ON_COMPLETION = "automatic_on_completion"
    MANUAL = "manual"

    CHOICES = (
        # "Automatic" governs only whether advancing to the next step needs a
        # second manual action once a human completes this step — it does NOT
        # auto-decide from review scores (min_reviewers / min_score_threshold
        # remain completion gates a human must clear).
        (
            AUTOMATIC_ON_COMPLETION,
            "Advance to the next step as soon as this one is completed",
        ),
        (MANUAL, "Hold for a separate manual advance after completion"),
    )


class AllocationTimes:
    """When a granted proposal's project/resources take effect.

    A call-level allocation *policy*, configured on the ``allocation_decision``
    workflow step: ``on_decision`` starts the allocation immediately, while
    ``fixed_date`` dates it to the round's ``allocation_date``.
    """

    ON_DECISION = "on_decision"
    FIXED_DATE = "fixed_date"

    CHOICES = (
        (ON_DECISION, "On decision"),
        (FIXED_DATE, "Fixed date"),
    )


class WorkflowStepOutcomes:
    """Allowed outcome values for completed workflow steps.

    Outcomes are validated against the call step's outcome set (see
    ``OutcomeSets``). ``REJECTED``
    and ``EXPIRED`` are reserved for system-driven transitions (reject endpoint,
    expiry task) and cannot be supplied via complete_workflow_step.
    """

    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    DECLINED = "declined"
    ACCEPTED = "accepted"
    SUBMITTED = "submitted"
    DONE = "done"
    REJECTED = "rejected"
    EXPIRED = "expired"
    # The applicant let a response step lapse without answering; the workflow
    # goes on as if they had answered.
    NO_RESPONSE = "no_response"

    CHOICES = (
        (ELIGIBLE, "Eligible"),
        (INELIGIBLE, "Ineligible"),
        (FEASIBLE, "Feasible"),
        (INFEASIBLE, "Infeasible"),
        (REVIEWED, "Reviewed"),
        (APPROVED, "Approved"),
        (DECLINED, "Declined"),
        (ACCEPTED, "Accepted"),
        (SUBMITTED, "Submitted"),
        (DONE, "Done"),
        (REJECTED, "Rejected"),
        (EXPIRED, "Expired"),
        (NO_RESPONSE, "No response"),
    )

    # System-only outcomes; complete_workflow_step rejects them.
    SYSTEM_RESERVED = frozenset({REJECTED, EXPIRED, NO_RESPONSE})

    # User-submittable outcomes that terminate the workflow negatively: the
    # proposal is rejected, or canceled where the step says so (on_negative),
    # instead of advancing / allocating. See workflow_service.complete_step.
    NEGATIVE_OUTCOMES = frozenset({INELIGIBLE, INFEASIBLE, DECLINED})


class OutcomeSets:
    """Named sets of outcomes a call step may be completed with."""

    ELIGIBILITY = "eligibility"
    FEASIBILITY = "feasibility"
    REVIEWED = "reviewed"
    APPROVAL = "approval"
    ACCEPTANCE = "acceptance"
    SUBMITTED = "submitted"
    DONE = "done"
    PER_OFFERING = "per_offering"

    CHOICES = (
        (ELIGIBILITY, "Eligible / ineligible"),
        (FEASIBILITY, "Feasible / infeasible"),
        (REVIEWED, "Reviewed"),
        (APPROVAL, "Approved / declined"),
        (ACCEPTANCE, "Accepted / declined"),
        (SUBMITTED, "Submitted"),
        (DONE, "Done"),
        (PER_OFFERING, "Per requested offering"),
    )

    OUTCOMES = {
        ELIGIBILITY: (WorkflowStepOutcomes.ELIGIBLE, WorkflowStepOutcomes.INELIGIBLE),
        FEASIBILITY: (WorkflowStepOutcomes.FEASIBLE, WorkflowStepOutcomes.INFEASIBLE),
        REVIEWED: (WorkflowStepOutcomes.REVIEWED,),
        APPROVAL: (WorkflowStepOutcomes.APPROVED, WorkflowStepOutcomes.DECLINED),
        ACCEPTANCE: (WorkflowStepOutcomes.ACCEPTED, WorkflowStepOutcomes.DECLINED),
        SUBMITTED: (WorkflowStepOutcomes.SUBMITTED,),
        DONE: (WorkflowStepOutcomes.DONE,),
        # Derived from the contributions' outcomes; see ContributionOutcomes.
        PER_OFFERING: (WorkflowStepOutcomes.FEASIBLE, WorkflowStepOutcomes.INFEASIBLE),
    }

    # Outcome sets whose step outcome is derived from the outcomes of its
    # contributions rather than picked by the completer.
    DERIVED = frozenset({PER_OFFERING})


class OnNegativeOutcomes:
    """What a negative outcome of a call step does to the proposal."""

    REJECT = "reject"
    CANCEL = "cancel"
    CONTINUE = "continue"

    CHOICES = (
        (REJECT, "Reject the proposal"),
        (CANCEL, "Cancel the proposal"),
        (CONTINUE, "Continue to the next step"),
    )


class ContributorSources:
    """Who contributes to a call step, each with a contribution of their own.

    ``proposal_role:<id>`` names an evaluator role of the call and
    ``call_role:<role>`` a role held on the call (``call_manager``,
    ``reviewer``, ``panel_member``, or ``panel_chair`` for the panel chair
    and vice-chair); see ``parse_contributor_source``.
    """

    NONE = "none"
    REQUESTED_OFFERING_TEAM = "requested_offering_team"
    ASSIGNED_REVIEWERS = "assigned_reviewers"
    PROPOSAL_ROLE = "proposal_role"
    CALL_ROLE = "call_role"

    # Sources that carry a parameter after a colon.
    PARAMETERISED = (PROPOSAL_ROLE, CALL_ROLE)
    PLAIN = (NONE, REQUESTED_OFFERING_TEAM, ASSIGNED_REVIEWERS)

    CHOICES = (
        (NONE, "Whoever completes the step"),
        (REQUESTED_OFFERING_TEAM, "The team of each requested offering"),
        (ASSIGNED_REVIEWERS, "Assigned reviewers"),
        (PROPOSAL_ROLE, "Holders of an evaluator role on the proposal"),
        (CALL_ROLE, "Holders of a role on the call"),
    )

    # Roles a call_role source may name.
    CALL_ROLES = ("call_manager", "reviewer", "panel_member", "panel_chair")


def parse_contributor_source(value):
    """Split a contributor source into ``(source, parameter)``.

    Raises ``ValueError`` for a value that names no known source.
    """
    value = value or ContributorSources.NONE
    source, _, parameter = value.partition(":")
    if source in ContributorSources.PLAIN and not parameter:
        return source, None
    if source in ContributorSources.PARAMETERISED and parameter:
        if (
            source == ContributorSources.CALL_ROLE
            and parameter not in ContributorSources.CALL_ROLES
        ):
            raise ValueError(
                f"'{parameter}' is not a call role; use one of "
                f"{', '.join(ContributorSources.CALL_ROLES)}."
            )
        return source, parameter
    raise ValueError(
        f"'{value}' is not a contributor source; use one of "
        "none, requested_offering_team, assigned_reviewers, "
        "proposal_role:<id> or call_role:<role>."
    )


class DelegationTargets:
    """Who the holder of a step's slot may hand it to.

    ``offering_team`` is the team of the slot's own offering on a step split
    per requested offering: anyone holding a role on it, its managers or a
    deployment's own offering roles such as a technical reviewer, which open
    nothing on the provider side unless they carry permissions.
    ``proposal_role:<id>``
    names an evaluator role of the call, whose holders on the proposal take
    over a person's slot (a rapporteur writing the panel chair's report).
    """

    NONE = ""
    OFFERING_TEAM = "offering_team"
    PROPOSAL_ROLE = "proposal_role"

    CHOICES = (
        (NONE, "Nobody"),
        (OFFERING_TEAM, "Anyone with a role on the slot's offering"),
        (PROPOSAL_ROLE, "Holders of an evaluator role on the proposal"),
    )


def parse_delegation_target(value):
    """Split a ``delegable_to`` value into ``(target, parameter)``.

    Raises ``ValueError`` for a value that names no known target.
    """
    value = value or DelegationTargets.NONE
    if value in (DelegationTargets.NONE, DelegationTargets.OFFERING_TEAM):
        return value, None
    target, _, parameter = value.partition(":")
    if target == DelegationTargets.PROPOSAL_ROLE and parameter:
        return target, parameter
    raise ValueError(
        f"'{value}' is not a delegation target; use offering_team or "
        "proposal_role:<id>, or leave it empty."
    )


def is_proposal_role_ref(parameter) -> bool:
    """Whether ``parameter`` names an evaluator role: a numeric id or a uuid."""
    if not parameter:
        return False
    if parameter.isdigit():
        return True
    try:
        uuid.UUID(parameter)
    except ValueError:
        return False
    return True


class StepContributionStatuses:
    """Where one contribution to a proposal's step stands.

    ``pending`` waits for its contributor, ``accepted`` is being worked on
    (a review accepted from an assignment), ``submitted`` is done. The rest
    no longer count: ``declined`` by its contributor, ``withdrawn`` by the
    call manager or with the step decided, ``expired`` with the step or with
    its contributor's standing to fill it -- their role revoked or lapsed, a
    conflict of interest recorded -- which comes back with that standing.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    SUBMITTED = "submitted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"

    CHOICES = (
        (PENDING, "Pending"),
        (ACCEPTED, "Accepted"),
        (SUBMITTED, "Submitted"),
        (DECLINED, "Declined"),
        (WITHDRAWN, "Withdrawn"),
        (EXPIRED, "Expired"),
    )

    # Still waiting for work.
    OPEN = frozenset({PENDING, ACCEPTED})
    # No longer part of the step's work.
    CLOSED = frozenset({DECLINED, WITHDRAWN, EXPIRED})


class ContributionOutcomes:
    """What one contribution to a per-offering assessment concludes."""

    ACCEPTED = "accepted"
    CONDITIONALLY_ACCEPTED = "conditionally_accepted"
    REJECTED = "rejected"

    CHOICES = (
        (ACCEPTED, "Accepted"),
        (CONDITIONALLY_ACCEPTED, "Conditionally accepted"),
        (REJECTED, "Rejected"),
    )

    NEGATIVE = frozenset({REJECTED})

    # Contribution outcomes per derived outcome set.
    BY_OUTCOME_SET = {
        OutcomeSets.PER_OFFERING: (ACCEPTED, CONDITIONALLY_ACCEPTED, REJECTED),
    }

    @classmethod
    def for_step(cls, outcome_set, allows_conditional_acceptance=True):
        """A step's contribution outcomes: a step that takes only a yes or
        a no (``allows_conditional_acceptance`` off) drops the conditional
        one."""
        outcomes = cls.BY_OUTCOME_SET.get(outcome_set, ())
        if allows_conditional_acceptance:
            return outcomes
        return tuple(o for o in outcomes if o != cls.CONDITIONALLY_ACCEPTED)


class StepKinds:
    APPLICANT_ACTION = "applicant_action"
    ASSIGNMENT = "assignment"
    ASSESSMENT = "assessment"
    DECISION = "decision"

    CHOICES = (
        (APPLICANT_ACTION, "Applicant action"),
        (ASSIGNMENT, "Assignment"),
        (ASSESSMENT, "Assessment"),
        (DECISION, "Decision"),
    )


# The one scale every review is scored on: its summary score and the score of
# each criterion of its step. Score gates, score spreads and the averages
# across a call compare reviews of different steps, so the scale is not
# configurable per step. A summary score of 0 means "not rated yet".
REVIEW_SCORE_MIN = 1
REVIEW_SCORE_MAX = 5
# A criterion's weight in the review's overall score, relative to the other
# criteria of its step.
CRITERION_WEIGHT_MAX = 100


class ReviewRejectionCauses:
    """Why a review was closed without being submitted."""

    # The reviewer, or staff on their behalf, declined it.
    DECLINED = "declined"
    # Its deadline passed before it was submitted.
    EXPIRED = "expired"
    # The reviewer joined the proposal's team, so it was withdrawn from them.
    WITHDRAWN = "withdrawn"

    CHOICES = (
        (DECLINED, "Declined by the reviewer"),
        (EXPIRED, "Expired at its deadline"),
        (WITHDRAWN, "Withdrawn when the reviewer joined the proposal team"),
    )


# =============================================================================
# Step kinds
# =============================================================================

# Call step fields every kind uses.
# A run condition on how far apart the scores of an earlier step's
# contributions lie (``CallWorkflowStep.run_condition``): the step runs when
# the highest and the lowest differ by more than this many points.
SCORE_SPREAD_ABOVE = "score_spread_above"
MAX_SCORE_SPREAD = 1000


COMMON_STEP_FIELDS = (
    "name",
    "description",
    "order",
    "is_enabled",
    "duration_in_days",
    "checklist",
    "checklist_required",
    "applicant_visible",
    "completer",
    "completer_proposal_role",
    "transition_mode",
    "outcome_set",
    "on_negative",
    "input_steps",
    "run_condition",
    "parallel_with_previous",
    "phase",
    "project_end_offset_days",
    "status_labels",
)

# Rules every new step gets: the completer and the call managers are warned a
# day before the step lapses and again when it does.
COMMON_NOTIFICATION_RULES = (
    (
        NotificationRuleTriggers.DEADLINE_APPROACHING,
        NotificationRuleRecipients.RESPONSIBLE_ROLE,
        1,
    ),
    (
        NotificationRuleTriggers.DEADLINE_APPROACHING,
        NotificationRuleRecipients.CALL_MANAGERS,
        1,
    ),
    (
        NotificationRuleTriggers.STEP_EXPIRED,
        NotificationRuleRecipients.RESPONSIBLE_ROLE,
        None,
    ),
    (
        NotificationRuleTriggers.STEP_EXPIRED,
        NotificationRuleRecipients.CALL_MANAGERS,
        None,
    ),
)

# A panel step's chair owns the panel's consolidated recommendation, so they
# get the same reminders as the members. Keyed by completer, not kind: any
# step the panel completes involves the chair.
PANEL_CHAIR_NOTIFICATION_RULES = (
    (
        NotificationRuleTriggers.DEADLINE_APPROACHING,
        NotificationRuleRecipients.PANEL_CHAIR,
        1,
    ),
    (
        NotificationRuleTriggers.STEP_EXPIRED,
        NotificationRuleRecipients.PANEL_CHAIR,
        None,
    ),
)

STEP_KINDS = [
    StepKindDefinition(
        id=StepKinds.APPLICANT_ACTION,
        name="Applicant action",
        description=(
            "The applicant fills in a form or answers, e.g. accepts or declines "
            "the award."
        ),
        outcome_sets=(OutcomeSets.SUBMITTED, OutcomeSets.ACCEPTANCE, OutcomeSets.DONE),
        default_completer=ResponsibleRoles.APPLICANT,
        # The applicant is reminded before their step lapses.
        default_notification_rules=(
            (
                NotificationRuleTriggers.STEP_STARTED,
                NotificationRuleRecipients.APPLICANT,
                None,
            ),
            (
                NotificationRuleTriggers.DEADLINE_APPROACHING,
                NotificationRuleRecipients.APPLICANT,
                1,
            ),
        ),
        applicant_facing=True,
    ),
    StepKindDefinition(
        id=StepKinds.ASSIGNMENT,
        name="Assignment",
        description="Someone picks the people who act on the proposal later on.",
        outcome_sets=(OutcomeSets.DONE,),
        default_completer=ResponsibleRoles.CALL_MANAGER,
        config_fields=(
            "assigns_roles",
            "requires_coi_confirmation",
            "coi_declaration_text",
        ),
    ),
    StepKindDefinition(
        id=StepKinds.ASSESSMENT,
        name="Assessment",
        description=(
            "Assigned people assess the proposal, e.g. a technical assessment "
            "or a peer review."
        ),
        outcome_sets=(
            OutcomeSets.REVIEWED,
            OutcomeSets.FEASIBILITY,
            OutcomeSets.DONE,
            OutcomeSets.PER_OFFERING,
        ),
        default_completer=ResponsibleRoles.REVIEWER,
        config_fields=(
            "blind_review",
            "requires_coi_confirmation",
            "coi_declaration_text",
            "contributor_source",
            "per_requested_offering",
            "allows_conditional_acceptance",
            "contributors_lead_only",
            "delegable_to",
            "min_contributions",
            "max_contributions",
            "min_score_threshold",
            "criteria",
            "release_to_applicant",
            "collects_reviewer_suggestions",
            "allows_change_requests",
            "round_scoped",
        ),
    ),
    StepKindDefinition(
        id=StepKinds.DECISION,
        name="Decision",
        description=(
            "One completer picks an outcome, e.g. an eligibility check, a panel "
            "recommendation or the allocation decision."
        ),
        outcome_sets=(OutcomeSets.APPROVAL, OutcomeSets.ELIGIBILITY),
        default_completer=ResponsibleRoles.CALL_MANAGER,
        config_fields=(
            "min_reviewers",
            "min_score_threshold",
            "is_allocation_decision",
            "decides_automatically",
            "allocation_time",
            "include_award_response",
            "allows_change_requests",
            "round_scoped",
        ),
    ),
]

STEP_KINDS_MAP = {kind.id: kind for kind in STEP_KINDS}

# The steps a new call starts with, in order; see DefaultCallStep.
DEFAULT_CALL_STEPS = [
    DefaultCallStep(
        key="administrative_check",
        kind=StepKinds.DECISION,
        name="Administrative check",
        description="Eligibility and completeness validation before evaluation.",
        outcome_set=OutcomeSets.ELIGIBILITY,
        completer=ResponsibleRoles.CALL_MANAGER,
        on_negative=OnNegativeOutcomes.REJECT,
    ),
    DefaultCallStep(
        key="technical_assessment",
        kind=StepKinds.ASSESSMENT,
        name="Technical assessment",
        description="Service provider verifies technical feasibility of the proposal.",
        outcome_set=OutcomeSets.FEASIBILITY,
        completer=ResponsibleRoles.OFFERING_MANAGER,
        on_negative=OnNegativeOutcomes.REJECT,
        contributor_source=ContributorSources.REQUESTED_OFFERING_TEAM,
    ),
    DefaultCallStep(
        key="expert_review",
        kind=StepKinds.ASSESSMENT,
        name="Expert review",
        description="Independent peer review by domain experts with scoring.",
        outcome_set=OutcomeSets.REVIEWED,
        completer=ResponsibleRoles.REVIEWER,
        on_negative=OnNegativeOutcomes.REJECT,
        contributor_source=ContributorSources.ASSIGNED_REVIEWERS,
    ),
    DefaultCallStep(
        key="panel_review",
        kind=StepKinds.DECISION,
        name="Panel review",
        description="Collective panel evaluation consolidating expert reviews.",
        outcome_set=OutcomeSets.APPROVAL,
        completer=ResponsibleRoles.PANEL_MEMBER,
        on_negative=OnNegativeOutcomes.REJECT,
    ),
    DefaultCallStep(
        key="allocation_decision",
        kind=StepKinds.DECISION,
        name="Allocation decision",
        description=(
            "Final decision to approve or reject the proposal and allocate resources."
        ),
        outcome_set=OutcomeSets.APPROVAL,
        completer=ResponsibleRoles.CALL_MANAGER,
        on_negative=OnNegativeOutcomes.REJECT,
        is_allocation_decision=True,
    ),
    DefaultCallStep(
        key="award_response",
        kind=StepKinds.APPLICANT_ACTION,
        name="Award response",
        description=(
            "Applicant explicitly accepts or declines the award before provisioning."
        ),
        outcome_set=OutcomeSets.ACCEPTANCE,
        completer=ResponsibleRoles.APPLICANT,
        on_negative=OnNegativeOutcomes.CANCEL,
    ),
]

DEFAULT_CALL_STEPS_MAP = {step.key: step for step in DEFAULT_CALL_STEPS}

# The applicant's answer to an award: added after the allocation decision by
# the decision's include_award_response flag rather than seeded on a new call.
AWARD_RESPONSE_KEY = "award_response"

# =============================================================================
# Call and Proposal States
# =============================================================================


class CallStates:
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"

    CHOICES = (
        (DRAFT, "Draft"),
        (ACTIVE, "Active"),
        (ARCHIVED, "Archived"),
    )


class RoundStatuses:
    SCHEDULED = "scheduled"
    OPEN = "open"
    ENDED = "ended"

    CHOICES = (
        (SCHEDULED, "Round is scheduled"),
        (OPEN, "Round is open"),
        (ENDED, "Round is ended"),
    )

    VALUES = [val for (val, _) in CHOICES]


class RoundLifecycleStates:
    """Where a round stands after its cut-off.

    Before the cut-off a round has no stored state: ``scheduled`` and ``open``
    are derived from its times (``RoundStatuses``). At the cut-off it enters
    ``evaluating``; the rest are call manager actions.
    """

    EVALUATING = "evaluating"
    DECIDING = "deciding"
    RESULTS_PUBLISHED = "results_published"
    CLOSED = "closed"

    CHOICES = (
        (EVALUATING, "Evaluating"),
        (DECIDING, "Deciding"),
        (RESULTS_PUBLISHED, "Results published"),
        (CLOSED, "Closed"),
    )

    # Decisions made in these states are announced as they are made.
    PUBLISHED = frozenset({RESULTS_PUBLISHED, CLOSED})
    # The round's ranking is what the decision body works from: fixed once
    # the round goes to it.
    RANKING_LOCKED = frozenset({DECIDING, RESULTS_PUBLISHED, CLOSED})


class ResultsPublication:
    """When applicants learn the outcome of the allocation decision.

    ``immediately`` announces each decision as it is made. ``with_round``
    records decisions but holds them until a call manager publishes the
    round's results, which announces them all at once.
    """

    IMMEDIATELY = "immediately"
    WITH_ROUND = "with_round"

    CHOICES = (
        (IMMEDIATELY, "Immediately"),
        (WITH_ROUND, "With the round"),
    )


class UndecidedAtRoundCompletion:
    """What completing a round does with proposals still without a decision.

    ``refuse`` refuses to complete the round until every proposal of it is
    decided. ``reject`` rejects each of them at the step it stands in, then
    completes the round.
    """

    REFUSE = "refuse"
    REJECT = "reject"

    CHOICES = (
        (REFUSE, "Refuse to complete the round"),
        (REJECT, "Reject them"),
    )


class BudgetEnforcement:
    """How a call holds its allocation decisions to its resource budgets.

    ``soft`` lets a call manager approve an award past what remains of a
    budget, with an explicit override and a reason. ``hard`` refuses such an
    award; only staff may override it, with a reason.
    """

    SOFT = "soft"
    HARD = "hard"

    CHOICES = (
        (SOFT, "Soft: approving past the budget needs an override"),
        (HARD, "Hard: an award past the budget is refused"),
    )


class OnDepletedBudget:
    """What allocating a round in submission order does with a proposal whose
    award no longer fits what remains of the budgets: decline it, or leave it
    for the call managers to decide by hand."""

    DECLINE = "decline"
    LEAVE = "leave"

    CHOICES = (
        (DECLINE, "Decline it"),
        (LEAVE, "Leave it undecided"),
    )


class EvaluationStart:
    """When a submitted proposal's review workflow starts.

    ``on_submission`` starts the first enabled step as soon as the applicant
    submits. ``at_cutoff`` holds every proposal in ``submitted`` until its
    round's cut-off, then starts them together, so a panel evaluates the whole
    batch of a cut-off at once and no check starts while the round is open.
    """

    ON_SUBMISSION = "on_submission"
    AT_CUTOFF = "at_cutoff"

    CHOICES = (
        (ON_SUBMISSION, "On submission"),
        (AT_CUTOFF, "At the round cut-off"),
    )


class ResourceTemplateSelectionModes:
    """How many of a call's resource templates one proposal may request.

    ``multiple`` lets an applicant combine templates. ``single`` treats them as
    mutually exclusive options, such as access tiers, so a proposal holds at
    most one resource request.
    """

    SINGLE = "single"
    MULTIPLE = "multiple"

    CHOICES = (
        (SINGLE, "Single"),
        (MULTIPLE, "Multiple"),
    )


class OrderAuthors:
    """Who the orders a call places when it grants resources are attributed to.

    Allocation authorises the spend through the call review, not through the
    person named here; this decides whose name the resulting order carries.
    That is who the service desk talks to when the offering is fulfilled by
    raising a helpdesk ticket, and who Waldur addresses its order mail to.
    """

    APPLICANT = "applicant"
    PROJECT_MANAGER = "project_manager"
    CALL_MANAGER = "call_manager"
    SPECIFIC_USER = "specific_user"

    CHOICES = (
        (APPLICANT, "Proposal applicant"),
        (PROJECT_MANAGER, "Project manager"),
        (CALL_MANAGER, "Call manager"),
        (SPECIFIC_USER, "Named contact"),
    )

    VALUES = [val for (val, _) in CHOICES]


class RequestedOfferingStates:
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    CANCELED = "canceled"

    CHOICES = (
        (REQUESTED, "Requested"),
        (ACCEPTED, "Accepted"),
        (CANCELED, "Canceled"),
    )


class ProposalStates:
    DRAFT = "draft"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELED = "canceled"

    CHOICES = (
        (DRAFT, "Draft"),
        (SUBMITTED, "Submitted"),
        (IN_REVIEW, "In review"),
        (ACCEPTED, "Accepted"),
        (REJECTED, "Rejected"),
        (CANCELED, "Canceled"),
    )

    # Terminal states: the allocation decision has been made, so the granted
    # duration is final. Everything else is still pending and keeps following
    # the call configuration.
    ALLOCATED_STATES = frozenset({ACCEPTED, REJECTED, CANCELED})

    # Submitted and not yet decided: the states in which a proposal may be
    # given to reviewers.
    UNDER_EVALUATION = frozenset({SUBMITTED, IN_REVIEW})


class ReviewStates:
    IN_REVIEW = "in_review"
    SUBMITTED = "submitted"
    REJECTED = "rejected"

    CHOICES = (
        (IN_REVIEW, "In review"),
        (SUBMITTED, "Submitted"),
        (REJECTED, "Rejected"),
    )


# Reviewer Profile Enums


class ReviewerAffiliationTypes:
    EMPLOYMENT = "employment"
    EDUCATION = "education"
    VISITING = "visiting"
    HONORARY = "honorary"
    CONSULTING = "consulting"

    CHOICES = (
        (EMPLOYMENT, "Employment"),
        (EDUCATION, "Education"),
        (VISITING, "Visiting position"),
        (HONORARY, "Honorary position"),
        (CONSULTING, "Consulting"),
    )


class ExpertiseProficiencyLevels:
    EXPERT = "expert"
    FAMILIAR = "familiar"
    BASIC = "basic"

    CHOICES = (
        (EXPERT, "Expert - Can review independently"),
        (FAMILIAR, "Familiar - Can review with guidance"),
        (BASIC, "Basic - General understanding only"),
    )


class PublicationVenueTypes:
    JOURNAL = "journal"
    CONFERENCE = "conference"
    PREPRINT = "preprint"
    BOOK = "book"
    THESIS = "thesis"
    REPORT = "report"
    OTHER = "other"

    CHOICES = (
        (JOURNAL, "Journal article"),
        (CONFERENCE, "Conference paper"),
        (PREPRINT, "Preprint"),
        (BOOK, "Book or book chapter"),
        (THESIS, "Thesis or dissertation"),
        (REPORT, "Technical report"),
        (OTHER, "Other"),
    )


# Conflict of Interest Enums


class COITypes:
    """COI type codes based on NIH, ERC, and industry standards."""

    # Real conflicts (must recuse)
    INST_SAME = "INST_SAME"
    FIN_DIRECT = "FIN_DIRECT"
    REL_FAMILY = "REL_FAMILY"
    ROLE_NAMED = "ROLE_NAMED"
    COLLAB_ACTIVE = "COLLAB_ACTIVE"
    REL_MENTOR = "REL_MENTOR"
    REL_SUPERVISOR = "REL_SUPERVISOR"

    # Apparent conflicts (requires management)
    COAUTH_RECENT = "COAUTH_RECENT"
    INST_DEPT = "INST_DEPT"
    INST_FORMER = "INST_FORMER"
    ROLE_CONF = "ROLE_CONF"
    COLLAB_GRANT = "COLLAB_GRANT"
    REL_EDITORIAL = "REL_EDITORIAL"
    COMPET = "COMPET"

    # Potential conflicts (disclosure only)
    COAUTH_OLD = "COAUTH_OLD"
    INST_CONSORT = "INST_CONSORT"
    CONF_ATTEND = "CONF_ATTEND"
    SOC_MEMBER = "SOC_MEMBER"

    CHOICES = (
        # Real conflicts
        (INST_SAME, "Same institution"),
        (FIN_DIRECT, "Direct financial interest"),
        (REL_FAMILY, "Family relationship"),
        (ROLE_NAMED, "Named on proposal"),
        (COLLAB_ACTIVE, "Active collaboration"),
        (REL_MENTOR, "Mentor/mentee relationship"),
        (REL_SUPERVISOR, "Supervisor relationship"),
        # Apparent conflicts
        (COAUTH_RECENT, "Recent co-authorship"),
        (INST_DEPT, "Same department"),
        (INST_FORMER, "Former institution"),
        (ROLE_CONF, "Conference organizer"),
        (COLLAB_GRANT, "Grant collaboration"),
        (REL_EDITORIAL, "Editorial relationship"),
        (COMPET, "Competitor"),
        # Potential conflicts
        (COAUTH_OLD, "Distant co-authorship"),
        (INST_CONSORT, "Consortium membership"),
        (CONF_ATTEND, "Conference participation"),
        (SOC_MEMBER, "Professional society"),
    )

    # Mapping from type to default severity
    SEVERITY_MAP = {
        INST_SAME: "real",
        FIN_DIRECT: "real",
        REL_FAMILY: "real",
        ROLE_NAMED: "real",
        COLLAB_ACTIVE: "real",
        REL_MENTOR: "real",
        REL_SUPERVISOR: "real",
        COAUTH_RECENT: "apparent",
        INST_DEPT: "apparent",
        INST_FORMER: "apparent",
        ROLE_CONF: "apparent",
        COLLAB_GRANT: "apparent",
        REL_EDITORIAL: "apparent",
        COMPET: "apparent",
        COAUTH_OLD: "potential",
        INST_CONSORT: "potential",
        CONF_ATTEND: "potential",
        SOC_MEMBER: "potential",
    }

    # Types that can be automatically detected
    AUTO_DETECT_TYPES = {
        INST_SAME,
        INST_DEPT,
        INST_FORMER,
        COAUTH_RECENT,
        COAUTH_OLD,
        ROLE_NAMED,
    }


class COISeverityLevels:
    REAL = "real"
    APPARENT = "apparent"
    POTENTIAL = "potential"

    CHOICES = (
        (REAL, "Real conflict - must recuse"),
        (APPARENT, "Apparent conflict - requires management"),
        (POTENTIAL, "Potential conflict - disclosure only"),
    )


class COIDetectionMethods:
    AUTOMATED = "automated"
    SELF_DISCLOSED = "self_disclosed"
    REPORTED = "reported"
    MANAGER_IDENTIFIED = "manager_identified"

    CHOICES = (
        (AUTOMATED, "Automated detection"),
        (SELF_DISCLOSED, "Self-disclosed by reviewer"),
        (REPORTED, "Reported by third party"),
        (MANAGER_IDENTIFIED, "Identified by call manager"),
    )


class COIStatuses:
    PENDING = "pending"
    DISMISSED = "dismissed"
    WAIVED = "waived"
    RECUSED = "recused"

    CHOICES = (
        (PENDING, "Pending review"),
        (DISMISSED, "Dismissed - not a conflict"),
        (WAIVED, "Waived with management plan"),
        (RECUSED, "Reviewer recused"),
    )


# Reviewer Pool Enums


class ReviewerStandings:
    """A reviewer's standing on the platform, set by its operator.

    Only an active reviewer is selected for, invited to or offered new review
    work; work already accepted is left to the call's managers to reassign.
    Suspended is meant to be lifted, blocked is not; both are reversible and
    neither deletes anything.
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"
    BLOCKED = "blocked"

    CHOICES = (
        (ACTIVE, "Active"),
        (SUSPENDED, "Suspended"),
        (BLOCKED, "Blocked"),
    )


class ReviewerPoolInvitationStatuses:
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"

    CHOICES = (
        (PENDING, "Invitation pending"),
        (ACCEPTED, "Accepted"),
        (DECLINED, "Declined"),
        (EXPIRED, "Invitation expired"),
    )


class ReviewerInvitationOutcomes:
    """What inviting a suggested reviewer to the call's pool came to."""

    INVITED = "invited"  # A new pool invitation was sent.
    RESENT = "resent"  # Their expired invitation was sent again.
    LINKED = "linked"  # Linked to the pool entry they already had.
    REFUSED = "refused"  # Not invited; the detail says why.

    CHOICES = (
        (INVITED, "Invited"),
        (RESENT, "Invitation sent again"),
        (LINKED, "Linked to their pool entry"),
        (REFUSED, "Not invited"),
    )


class ProposalReviewerSuggestionStatuses:
    """Where a reviewer suggested for a proposal stands.

    ``pending`` until the call's managers act on it; then the pool invitation
    it became decides: ``invited`` while unanswered, ``accepted``,
    ``declined`` or ``expired``. ``discarded`` when not taken up.
    """

    PENDING = "pending"
    INVITED = "invited"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    DISCARDED = "discarded"

    CHOICES = (
        (PENDING, "Pending"),
        (INVITED, "Invited"),
        (ACCEPTED, "Accepted"),
        (DECLINED, "Declined"),
        (EXPIRED, "Invitation expired"),
        (DISCARDED, "Discarded"),
    )


# COI Disclosure Enums


class FinancialInterestEntityTypes:
    COMPANY = "company"
    STARTUP = "startup"
    NONPROFIT = "nonprofit"
    GOVERNMENT = "government"
    OTHER = "other"

    CHOICES = (
        (COMPANY, "Company"),
        (STARTUP, "Startup"),
        (NONPROFIT, "Non-profit organization"),
        (GOVERNMENT, "Government entity"),
        (OTHER, "Other"),
    )


class FinancialInterestRelationshipTypes:
    EMPLOYMENT = "employment"
    CONSULTING = "consulting"
    EQUITY = "equity"
    BOARD = "board"
    ROYALTIES = "royalties"
    GIFTS = "gifts"
    OTHER = "other"

    CHOICES = (
        (EMPLOYMENT, "Employment"),
        (CONSULTING, "Consulting"),
        (EQUITY, "Equity/stock ownership"),
        (BOARD, "Board membership"),
        (ROYALTIES, "Royalties/licensing"),
        (GIFTS, "Gifts/honoraria"),
        (OTHER, "Other"),
    )


class FinancialInterestAmountRanges:
    NONE = "none"
    UNDER_5K = "under_5k"
    RANGE_5K_10K = "5k_10k"
    RANGE_10K_50K = "10k_50k"
    OVER_50K = "over_50k"

    CHOICES = (
        (NONE, "None"),
        (UNDER_5K, "Under $5,000"),
        (RANGE_5K_10K, "$5,000 - $10,000"),
        (RANGE_10K_50K, "$10,000 - $50,000"),
        (OVER_50K, "Over $50,000"),
    )


# Matching Enums


class MatchingAffinityMethods:
    KEYWORD = "keyword"
    TFIDF = "tfidf"
    COMBINED = "combined"

    CHOICES = (
        (KEYWORD, "Keyword matching only"),
        (TFIDF, "TF-IDF text similarity"),
        (COMBINED, "Combined keyword + text"),
    )


class MatchingAlgorithms:
    MINMAX = "minmax"
    FAIRFLOW = "fairflow"
    HUNGARIAN = "hungarian"

    CHOICES = (
        (MINMAX, "MinMax (balanced load)"),
        (FAIRFLOW, "FairFlow (quality threshold)"),
        (HUNGARIAN, "Hungarian (global optimum)"),
    )


class COIDetectionJobTypes:
    FULL_CALL = "full_call"
    INCREMENTAL = "incremental"
    SINGLE_PAIR = "single_pair"

    CHOICES = (
        (FULL_CALL, "Full call detection"),
        (INCREMENTAL, "Incremental detection"),
        (SINGLE_PAIR, "Single pair detection"),
    )


class COIDetectionJobStates:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    CHOICES = (
        (PENDING, "Pending"),
        (RUNNING, "Running"),
        (COMPLETED, "Completed"),
        (FAILED, "Failed"),
        (CANCELLED, "Cancelled"),
    )


class ReviewerBidChoices:
    """Choices for reviewer bids on proposals."""

    EAGER = "eager"
    WILLING = "willing"
    NOT_WILLING = "not_willing"
    CONFLICT = "conflict"

    CHOICES = (
        (EAGER, "Eager to review"),
        (WILLING, "Willing to review"),
        (NOT_WILLING, "Not willing to review"),
        (CONFLICT, "Has conflict of interest"),
    )

    # Weights for matching algorithm (higher = better match preference)
    WEIGHTS = {
        EAGER: 1.0,
        WILLING: 0.5,
        NOT_WILLING: -0.5,
        CONFLICT: -1.0,
    }


class ReviewerSuggestionStatuses:
    """Statuses for algorithm-generated reviewer suggestions."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INVITED = "invited"

    CHOICES = (
        (PENDING, "Awaiting manager review"),
        (CONFIRMED, "Manager approved"),
        (REJECTED, "Manager rejected"),
        (INVITED, "Invitation sent"),
    )


class ProposalDisclosureLevels:
    """Level of proposal information disclosed in reviewer invitations."""

    TITLES_ONLY = "titles_only"
    TITLES_AND_SUMMARIES = "titles_and_summaries"
    FULL_DETAILS = "full_details"

    CHOICES = (
        (TITLES_ONLY, "Titles only"),
        (TITLES_AND_SUMMARIES, "Titles and summaries"),
        (FULL_DETAILS, "Full proposal details"),
    )


class AffinityMatrixScopes:
    """Scope filters for affinity matrix retrieval."""

    POOL = "pool"
    SUGGESTIONS = "suggestions"
    ALL = "all"

    CHOICES = (
        (POOL, "Pool members only"),
        (SUGGESTIONS, "Suggested reviewers only"),
        (ALL, "All reviewers"),
    )

    VALUES = [val for (val, _) in CHOICES]


class AffinityMatrixSources:
    """Source indicators for affinity matrix entries."""

    POOL = "pool"
    SUGGESTION = "suggestion"

    CHOICES = (
        (POOL, "Pool member"),
        (SUGGESTION, "Suggested reviewer"),
    )


# =============================================================================
# Assignment Batch Enums (Stage 2 - Proposal Assignment Workflow)
# =============================================================================


class AssignmentBatchStatuses:
    """Status for assignment batches sent to reviewers."""

    DRAFT = "draft"
    SENT = "sent"
    RESPONDED = "responded"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    CHOICES = (
        (DRAFT, "Draft - pending approval"),
        (SENT, "Sent to reviewer"),
        (RESPONDED, "Reviewer responded"),
        (EXPIRED, "Invitation expired"),
        (CANCELLED, "Cancelled by manager"),
    )

    # Batches the reviewer has actually received. A draft is still the call
    # manager's work in progress and a cancelled batch was withdrawn, so
    # neither is shown to the reviewer.
    SENT_TO_REVIEWER = (SENT, RESPONDED, EXPIRED)


class AssignmentItemStatuses:
    """Status for individual proposal assignments within a batch."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    COI_BLOCKED = "coi_blocked"
    EXPIRED = "expired"
    REASSIGNED = "reassigned"

    CHOICES = (
        (PENDING, "Awaiting response"),
        (ACCEPTED, "Accepted"),
        (DECLINED, "Declined"),
        (COI_BLOCKED, "Blocked due to COI"),
        (EXPIRED, "Expired without response"),
        (REASSIGNED, "Reassigned to another reviewer"),
    )


class AssignmentSources:
    """Source of assignment creation."""

    ALGORITHM = "algorithm"
    MANUAL = "manual"

    CHOICES = (
        (ALGORITHM, "Algorithm-generated"),
        (MANUAL, "Manually assigned"),
    )


class SuggestionSourceTypes:
    """Source types for reviewer suggestion generation."""

    CALL_DESCRIPTION = "call_description"
    ALL_PROPOSALS = "all_proposals"
    SELECTED_PROPOSALS = "selected_proposals"
    CUSTOM_KEYWORDS = "custom_keywords"

    CHOICES = (
        (CALL_DESCRIPTION, "Call description"),
        (ALL_PROPOSALS, "All proposals"),
        (SELECTED_PROPOSALS, "Selected proposals"),
        (CUSTOM_KEYWORDS, "Custom keywords"),
    )


class KeywordSearchModes:
    """Search modes for custom keyword matching."""

    EXPERTISE_ONLY = "expertise_only"
    FULL_TEXT = "full_text"

    CHOICES = (
        (EXPERTISE_ONLY, "Match against reviewer expertise keywords"),
        (FULL_TEXT, "Search all reviewer content"),
    )


class BulkRoundCadence:
    """Cadence options for the rounds_bulk_set action.

    ``CHOICES`` is fed to the request serializer's ``ChoiceField`` and
    therefore propagates to the OpenAPI schema as ``CadenceEnum``.
    ``INTERVAL_MONTHS`` maps each non-custom value to the number of months
    between successive rounds. ``CUSTOM`` is intentionally absent from
    ``INTERVAL_MONTHS`` because its interval comes from the request body's
    ``custom_interval_months`` field.
    """

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    BIANNUAL = "biannual"
    YEARLY = "yearly"
    CUSTOM = "custom"

    CHOICES = (
        (MONTHLY, "Monthly"),
        (QUARTERLY, "Quarterly"),
        (BIANNUAL, "Biannual"),
        (YEARLY, "Yearly"),
        (CUSTOM, "Custom interval"),
    )

    INTERVAL_MONTHS = {
        MONTHLY: 1,
        QUARTERLY: 3,
        BIANNUAL: 6,
        YEARLY: 12,
    }


class ProposalFieldStates:
    """How a call treats one Project details field on the submission form.

    Ordered by how much the call demands of the applicant: ``HIDDEN`` is not
    asked at all, ``OPTIONAL`` is asked, ``REQUIRED`` blocks submission while
    empty. ``DEMAND`` gives that order a number so the locking rule can compare
    two states without spelling out every pair.
    """

    HIDDEN = "hidden"
    OPTIONAL = "optional"
    REQUIRED = "required"

    CHOICES = (
        (HIDDEN, "Hidden"),
        (OPTIONAL, "Optional"),
        (REQUIRED, "Required"),
    )

    DEMAND = {
        HIDDEN: 0,
        OPTIONAL: 1,
        REQUIRED: 2,
    }


class ProposalFieldUsages:
    """Where a Project details field is consumed once it is collected.

    Surfaced per field on the call configuration API so a call manager can see
    what switching a field off costs before switching it off. The keys are
    stable; the human-readable labels live in the frontend, next to the rest of
    the translated UI copy.
    """

    APPLICANT_FORM = "applicant_form"
    REVIEWER_COMMENT = "reviewer_comment"
    REVIEWER_MATCHING = "reviewer_matching"
    MANAGER_LISTS = "manager_lists"
    AI_ASSISTANT = "ai_assistant"
    EXPORT_IMPORT = "export_import"

    CHOICES = (
        (APPLICANT_FORM, "Applicant form"),
        (REVIEWER_COMMENT, "Reviewer field comments"),
        (REVIEWER_MATCHING, "Automatic reviewer matching"),
        (MANAGER_LISTS, "Call manager lists"),
        (AI_ASSISTANT, "AI assistant context"),
        (EXPORT_IMPORT, "Export and import"),
    )


# Every configurable Project details field, with the consumers traced in
# waldur/waldur-mastermind#291. `name` is not here: it is always required, and
# `allocate_proposal` builds the granted project's name from the call, the
# round's start date and it. The project length is not a form field either: it
# is derived at allocation from the requested resources'
# `attributes.prepaid_duration_months`, else from `call.fixed_duration_in_days`.
PROPOSAL_CONFIGURABLE_FIELDS = {
    "project_summary": (
        ProposalFieldUsages.APPLICANT_FORM,
        ProposalFieldUsages.REVIEWER_COMMENT,
        ProposalFieldUsages.REVIEWER_MATCHING,
        ProposalFieldUsages.MANAGER_LISTS,
        ProposalFieldUsages.AI_ASSISTANT,
        ProposalFieldUsages.EXPORT_IMPORT,
    ),
    "description": (
        ProposalFieldUsages.APPLICANT_FORM,
        ProposalFieldUsages.REVIEWER_COMMENT,
        ProposalFieldUsages.REVIEWER_MATCHING,
        ProposalFieldUsages.EXPORT_IMPORT,
    ),
    "science_sub_domain": (
        ProposalFieldUsages.APPLICANT_FORM,
        ProposalFieldUsages.MANAGER_LISTS,
        ProposalFieldUsages.EXPORT_IMPORT,
    ),
    "supporting_documentation": (
        ProposalFieldUsages.APPLICANT_FORM,
        ProposalFieldUsages.REVIEWER_COMMENT,
    ),
}
