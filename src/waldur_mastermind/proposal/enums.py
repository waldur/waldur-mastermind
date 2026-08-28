from .structures import WorkflowStepDefinition

# =============================================================================
# Workflow Step Definitions
# =============================================================================

WORKFLOW_STEPS = [
    WorkflowStepDefinition(
        id="administrative_check",
        name="Administrative check",
        description="Eligibility and completeness validation before evaluation.",
        default_responsible_role="call_manager",
    ),
    WorkflowStepDefinition(
        id="technical_assessment",
        name="Technical assessment",
        description="Service provider verifies technical feasibility of the proposal.",
        default_responsible_role="offering_manager",
    ),
    WorkflowStepDefinition(
        id="expert_review",
        name="Expert review",
        description="Independent peer review by domain experts with scoring.",
        default_responsible_role="reviewer",
    ),
    WorkflowStepDefinition(
        id="panel_review",
        name="Panel review",
        description="Collective panel evaluation consolidating expert reviews.",
        dependencies=["expert_review"],
        default_responsible_role="panel_member",
    ),
    WorkflowStepDefinition(
        id="allocation_decision",
        name="Allocation decision",
        description="Final decision to approve or reject the proposal and allocate resources.",
        is_mandatory=True,
        default_responsible_role="call_manager",
    ),
    WorkflowStepDefinition(
        id="award_response",
        name="Award response",
        description="Applicant explicitly accepts or declines the award before provisioning.",
        default_responsible_role="applicant",
    ),
]

WORKFLOW_STEPS_MAP = {s.id: s for s in WORKFLOW_STEPS}
WORKFLOW_STEPS_CHOICES = [(s.id, s.name) for s in WORKFLOW_STEPS]
MANDATORY_STEPS = [s.id for s in WORKFLOW_STEPS if s.is_mandatory]
STEP_DEPENDENCIES = {s.id: s.dependencies for s in WORKFLOW_STEPS if s.dependencies}


class WorkflowStepInstanceStatuses:
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    EXPIRED = "expired"
    SKIPPED = "skipped"

    CHOICES = (
        (PENDING, "Pending"),
        (ACTIVE, "Active"),
        (COMPLETED, "Completed"),
        (EXPIRED, "Expired"),
        (SKIPPED, "Skipped"),
    )


class ResponsibleRoles:
    CALL_MANAGER = "call_manager"
    OFFERING_MANAGER = "offering_manager"
    REVIEWER = "reviewer"
    PANEL_MEMBER = "panel_member"
    APPLICANT = "applicant"

    CHOICES = (
        (CALL_MANAGER, "Call manager"),
        (OFFERING_MANAGER, "Offering manager"),
        (REVIEWER, "Reviewer"),
        (PANEL_MEMBER, "Panel member"),
        (APPLICANT, "Applicant"),
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

    Outcomes are validated per-step against ``STEP_ALLOW_LIST``. ``REJECTED``
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
    REJECTED = "rejected"
    EXPIRED = "expired"

    CHOICES = (
        (ELIGIBLE, "Eligible"),
        (INELIGIBLE, "Ineligible"),
        (FEASIBLE, "Feasible"),
        (INFEASIBLE, "Infeasible"),
        (REVIEWED, "Reviewed"),
        (APPROVED, "Approved"),
        (DECLINED, "Declined"),
        (ACCEPTED, "Accepted"),
        (REJECTED, "Rejected"),
        (EXPIRED, "Expired"),
    )

    # System-only outcomes; complete_workflow_step rejects them.
    SYSTEM_RESERVED = frozenset({REJECTED, EXPIRED})

    # User-submittable outcomes that terminate the workflow negatively: the
    # proposal is rejected (or, for an applicant declining the award, canceled)
    # instead of advancing / allocating. See workflow_service.complete_step.
    NEGATIVE_OUTCOMES = frozenset({INELIGIBLE, INFEASIBLE, DECLINED})

    # Outcomes a user may submit when completing each step.
    STEP_ALLOW_LIST = {
        "administrative_check": frozenset({ELIGIBLE, INELIGIBLE}),
        "technical_assessment": frozenset({FEASIBLE, INFEASIBLE}),
        "expert_review": frozenset({REVIEWED}),
        "panel_review": frozenset({APPROVED, DECLINED}),
        "allocation_decision": frozenset({APPROVED, DECLINED}),
        "award_response": frozenset({ACCEPTED, DECLINED}),
    }


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
# round's start date and it. `duration_in_days` is not here either: a call must
# state the length of what it awards.
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
