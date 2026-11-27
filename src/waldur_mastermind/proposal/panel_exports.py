"""The evaluations of a call's proposals as spreadsheets: every contribution
to its steps, and a sheet per panel for the panel's meeting.

Most of an evaluation is not a ``Review``: individual and consolidated
reports, technical assessments with their outcome and conditions, are step
contributions and the answers to their steps' forms. ``evaluation_rows``
writes one row per contribution, with a column per question of the steps'
forms; ``panel_sheet_rows`` one row per proposal routed to a panel, with what
it asks for, its review scores, the outcome of each assessment step and the
technical assessment of each requested offering.

Both are read by the call's managers and by the chair and members of a panel,
for that panel. Neither tells a reader more than the screens do
(``Viewer``): a contribution is a row only for whoever may read it, a blind
step names its contributors by their anonymous label, and a panel member gets
no row for a proposal they have a conflict of interest with or are on the
applicant team of.

The columns are declared in lists of ``Column`` -- ``EVALUATION_COLUMNS``,
``PANEL_SHEET_COLUMNS`` -- and the groups derived from the call (requested
amounts, criteria, steps, offerings, questions) in the functions after them:
the sheet's layout is changed there and nowhere else. The writers are the
call exports' own (``exports.csv_response``, ``exports.xlsx_response``), so
cells are escaped as in every other export.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from django.db.models import Exists, OuterRef, Prefetch, Q
from django.utils.translation import gettext as _

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import QuestionTypes
from waldur_core.core.permissions import has_staff_access
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import (
    check_pat_support_scope,
    has_permission_on_any_source,
)
from waldur_mastermind.proposal import (
    contributions,
    exports,
    managers,
    models,
    proposal_roles,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.applicant_visibility import ApplicantVisibility
from waldur_mastermind.proposal.enums import (
    ContributionOutcomes,
    RequestedOfferingStates,
    StepContributionStatuses,
    StepKinds,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.serializers import (
    ProposalWorkflowStepInstanceSerializer,
)

# Proposals read per batch: their contributions, answers and steps are
# loaded a batch at a time, so the queries stay few and what is held stays
# bounded however large the call.
BATCH_SIZE = 50

# A question is a column; its text is its header, cut to stay readable.
QUESTION_HEADER_LENGTH = 120

OUTCOME_LABELS = {
    **dict(WorkflowStepOutcomes.CHOICES),
    **dict(ContributionOutcomes.CHOICES),
}
STEP_STATUS_LABELS = dict(WorkflowStepInstanceStatuses.CHOICES)
CONTRIBUTION_STATUS_LABELS = dict(StepContributionStatuses.CHOICES)


def outcome_label(outcome) -> str:
    return OUTCOME_LABELS.get(outcome, outcome or "")


# =============================================================================
# Who reads what
# =============================================================================


def acts_for_whole_platform(request) -> bool:
    """Staff, and global support: they read every proposal, their own
    included, as the other call exports let them."""
    user = request.user
    return has_staff_access(request) or (
        user.is_support and check_pat_support_scope(request)
    )


def manages_call(request, call) -> bool:
    """Staff, global support, and whoever may update the call: they read the
    whole call, as the other call exports do."""
    if acts_for_whole_platform(request):
        return True
    return has_permission_on_any_source(
        request,
        PermissionEnum.UPDATE_CALL,
        call,
        proposal_permissions.CALL_PERMISSION_SOURCES,
    )


class Viewer:
    """What one reader is shown of a call's evaluations, worked out once per
    export, and per call, step or proposal where the rule allows.

    The call's managers read every proposal, every contribution and every
    outcome, as on the call's pages -- but for the proposals whose applicant
    team they are on, which they read as applicants and which the exports
    leave out for them. Anyone else -- a panel's chair or member -- reads what
    the proposal and step pages show them: the proposals they may open, less
    those they have a conflict of interest with or are on the team of; the
    contributions they may read (``contributions.Access``), under an
    anonymous label where the step is blind; their own reviews' scores and
    the others' only where the scores are opened to them; the step outcomes
    the step list shows them; and the applicant's details the call's
    visibility settings expose to evaluators.

    Every proposal the exports write has gone through ``proposals``, so the
    reader is on none of their teams: what depends on the proposal only
    through that is worked out once per call.
    """

    def __init__(self, request, call):
        self.user = request.user
        self.call = call
        self.whole_platform = acts_for_whole_platform(request)
        self.manages = manages_call(request, call)
        self.access = contributions.access_for(request)
        self._applicant_visibility = ApplicantVisibility(self.user)
        self._labels = {}
        self._memo = {}

    def _cached(self, key, compute):
        if key not in self._memo:
            self._memo[key] = compute()
        return self._memo[key]

    def proposals(self, queryset):
        """``queryset`` narrowed to the proposals the reader may export.

        Never, but for staff and support, a proposal whose applicant team the
        reader is on: on it they are the applicant, whatever their role on
        the call, and the screens keep its evaluation from them.
        """
        if self.whole_platform:
            return queryset
        queryset = queryset.exclude(
            pk__in=proposal_roles.applicant_team_proposal_ids(self.user)
        )
        if self.manages:
            return queryset
        return queryset.filter(models.reads_whole_proposal_q(self.user)).exclude(
            Exists(
                models.ConflictOfInterest.objects.filter(
                    reviewer__user=self.user,
                    proposal_id=OuterRef("pk"),
                    status__in=proposal_roles.BLOCKING_COI_STATUSES,
                )
            )
        )

    def take(self, proposal_ids):
        """Note the proposals ``proposals`` let through: the reader is on
        none of their applicant teams, so ``contributions.Access`` need not
        ask again for each."""
        if not self.whole_platform:
            self.access.assume_off_applicant_teams(proposal_ids)

    def concealed(self, proposal) -> set[str]:
        """The applicant attributes the call keeps from the reader."""
        if self.manages:
            return set()
        return self._applicant_visibility.concealed_attributes(proposal) or set()

    def reads_contribution(self, contribution) -> bool:
        return self.access.can_read(contribution)

    def contributor(self, contribution) -> str:
        """Who the contribution is by, as the reader may know them: by full
        name, as the proposal page names them."""
        if not contribution.contributor_id:
            return ""
        proposal = contribution.proposal_step.proposal
        if self.access.conceals_contributor(contribution):
            if proposal.pk not in self._labels:
                self._labels[proposal.pk] = contributions.anonymous_labels(proposal)
            return contributions.anonymous_label(
                contribution, self._labels[proposal.pk]
            )
        if "full_name" in self.concealed(proposal) and (
            contribution.contributor_id in self._applicant_team(proposal)
        ):
            return _("Applicant")
        return contribution.contributor.full_name

    def _applicant_team(self, proposal) -> set[int]:
        return self._cached(
            ("team", proposal.pk),
            lambda: proposal_roles.applicant_team_user_ids(proposal),
        )

    def _readable_review_ids(self) -> frozenset:
        """The call's reviews the reader may open (``filter_reviews_for_user``)."""
        return self._cached(
            "reviews",
            lambda: frozenset(
                managers.filter_reviews_for_user(self.user)
                .filter(proposal__round__call=self.call)
                .values_list("pk", flat=True)
            ),
        )

    def reads_review_scores(self, proposal) -> bool:
        """Whether the reader sees ``proposal``'s review scores together, as
        ``permissions.user_can_view_review_scores`` decides: the call team's
        scores, else only when every submitted review is one they may open
        anyway. Its call-team half depends on the proposal only through the
        applicant team, so it is asked once per call."""
        if self.manages:
            return True
        if self._cached(
            "step_scores",
            lambda: proposal_permissions.user_can_view_step_scores(
                self.user, proposal, staff=self.access.staff
            ),
        ):
            return True
        if self.user.is_staff:
            # Not acting as staff: filter_reviews_for_user reads the flag.
            return False
        readable = self._readable_review_ids()
        return all(
            review.pk in readable
            for review in proposal.review_set.all()
            if review.state == models.Review.States.SUBMITTED
        )

    def review_score(self, review):
        """The overall score of a submitted ``review``, where the reader is
        shown it: the call team; the reviewer, their own; anyone else only
        what they may open and while the scores are opened to them -- one
        reviewer's score next to the others' tells whose is whose."""
        if review is None or review.state != models.Review.States.SUBMITTED:
            return ""
        if self.manages or review.reviewer_id == self.user.pk:
            return review.summary_score
        if review.pk in self._readable_review_ids() and self.reads_review_scores(
            review.proposal
        ):
            return review.summary_score
        return ""

    def decision_withheld(self, proposal) -> bool:
        """Whether the allocation decision is held from the reader until the
        round's results are published: the step list then shows it running."""
        if self.manages or not proposal.decision_held:
            return False
        return self._cached(
            ("held", proposal.pk),
            lambda: not proposal_permissions.user_can_view_held_decision(
                self.user, proposal
            ),
        )

    def reads_step_outcome(self, instance) -> bool:
        """Whether the reader is shown the outcome of ``instance``, as the
        proposal's step list shows it to them: a review step's verdict only
        to those taking part in the step, unless the call shows reviews to
        submitters; any other step's to everyone reading the proposal."""
        if self.manages:
            return True
        proposal = instance.proposal
        if instance.is_allocation_decision and self.decision_withheld(proposal):
            return False
        if not ProposalWorkflowStepInstanceSerializer._is_review_step(instance):
            return True
        if proposal.round.call.reviews_visible_to_submitters:
            return True
        return self._cached(
            ("notes", instance.pk),
            lambda: proposal_permissions.user_can_view_internal_notes(
                self.user, proposal, instance, self.access
            ),
        )


def unique_headers(headers) -> list[str]:
    """``headers`` told apart: a repeated one -- the same question twice in a
    form, two offerings of one name -- gets " (2)", " (3)" and so on."""
    seen: dict[str, int] = {}
    taken = set(headers)
    result = []
    for header in headers:
        count = seen.get(header, 0) + 1
        seen[header] = count
        if count == 1:
            result.append(header)
            continue
        candidate = f"{header} ({count})"
        while candidate in taken:
            count += 1
            candidate = f"{header} ({count})"
        seen[header] = count
        taken.add(candidate)
        result.append(candidate)
    return result


# =============================================================================
# Columns
# =============================================================================


@dataclass(frozen=True)
class Column:
    """One column of a sheet: its header, and its cell for a row."""

    header: str
    value: Callable[["Row"], object]


@dataclass
class Row:
    """What one row is written from: a proposal, and a contribution to one of
    its steps on the evaluations export."""

    proposal: models.Proposal
    contribution: models.StepContribution | None = None
    viewer: Viewer | None = None
    extra: dict = field(default_factory=dict)


def _concealable(attribute, value):
    """A cell holding the applicant's ``attribute``, blank where the call
    keeps it from the reader."""

    def read(row: Row):
        if attribute in row.viewer.concealed(row.proposal):
            return ""
        return exports._text(value(row.proposal))

    return read


def _round(row: Row) -> str:
    return row.proposal.round.slug or row.proposal.round.name


def _panel(row: Row) -> str:
    return row.proposal.panel.name if row.proposal.panel_id else ""


# The evaluations export: one row per contribution. The question columns of
# the steps' forms follow these.
EVALUATION_COLUMNS = [
    Column("Proposal ID", lambda row: row.proposal.slug),
    Column("Proposal", lambda row: row.proposal.name),
    Column("Round", _round),
    Column("Panel", _panel),
    Column(
        "Step",
        lambda row: row.contribution.proposal_step.call_step.name
        or row.contribution.proposal_step.step,
    ),
    Column(
        "Offering",
        lambda row: row.contribution.requested_offering.offering.name
        if row.contribution.requested_offering_id
        else "",
    ),
    Column("Contributor", lambda row: row.viewer.contributor(row.contribution)),
    Column("Lead", lambda row: _("Yes") if row.contribution.is_lead else ""),
    Column(
        "Status",
        lambda row: CONTRIBUTION_STATUS_LABELS.get(
            row.contribution.status, row.contribution.status
        ),
    ),
    Column("Submitted", lambda row: exports._timestamp(row.contribution.submitted_at)),
    Column("Outcome", lambda row: outcome_label(row.contribution.outcome)),
    Column("Conditions", lambda row: row.contribution.conditions),
    Column("Comment", lambda row: row.contribution.comment),
    # The overall score of the review the contribution stands for, once it is
    # submitted; its criteria are on the reviews export.
    Column(
        "Review score",
        lambda row: row.viewer.review_score(row.contribution.review)
        if row.contribution.review_id
        else "",
    ),
]

# The panel sheet: one row per proposal routed to the panel. The requested
# amounts, review scores, step outcomes and technical assessments follow.
PANEL_SHEET_COLUMNS = [
    Column("Proposal ID", lambda row: row.proposal.slug),
    Column("Name", lambda row: row.proposal.name),
    Column("Round", _round),
    Column(
        "Applicant",
        _concealable("full_name", lambda p: p.created_by and p.created_by.full_name),
    ),
    Column(
        "Organisation",
        _concealable(
            "organization", lambda p: p.created_by and p.created_by.organization
        ),
    ),
    Column(
        "Science sub-domain",
        lambda row: exports._text(
            row.proposal.science_sub_domain and row.proposal.science_sub_domain.name
        ),
    ),
    Column("State", lambda row: row.proposal.get_state_display()),
    Column("Workflow step", lambda row: exports._step_label(row.proposal)),
    Column(
        "Requested duration",
        lambda row: row.extra["schema"].duration_label(row.proposal),
    ),
]


# =============================================================================
# The groups of columns a call's configuration decides
# =============================================================================


def _cut(text: str, length: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[: length - 1] + "…"


@dataclass(frozen=True)
class QuestionColumn:
    call_step_id: int
    question: checklist_models.Question
    header: str


def question_columns(call, call_step=None, step_ids=None) -> list[QuestionColumn]:
    """A column per question of the form of each of the call's steps --
    of ``call_step`` alone when the export is limited to it, of the steps
    ``step_ids`` names when given.

    Keyed on the step as well as the question: two steps sharing a form are
    answered apart, and their answers stay in columns of their own. A
    question is headed by its text, and by its step's name as well unless
    the export covers one step. The steps with a form are those enabled or
    already holding contributions, so a step switched off after it was
    worked on keeps its answers in the file.
    """
    steps = (
        models.CallWorkflowStep.objects.filter(call=call, checklist__isnull=False)
        .filter(
            Q(is_enabled=True)
            | Exists(
                models.StepContribution.objects.filter(
                    proposal_step__call_step=OuterRef("pk")
                )
            )
        )
        .order_by("order", "id")
    )
    if call_step is not None:
        steps = steps.filter(pk=call_step.pk)
    if step_ids is not None:
        steps = steps.filter(pk__in=step_ids)
    steps = list(steps)
    questions: dict[int, list] = {}
    for question in (
        checklist_models.Question.objects.filter(
            checklist_id__in={step.checklist_id for step in steps}
        )
        .prefetch_related("question_options")
        .order_by("order", "id")
    ):
        questions.setdefault(question.checklist_id, []).append(question)
    columns = []
    for step in steps:
        for question in questions.get(step.checklist_id, []):
            text = _cut(question.description, QUESTION_HEADER_LENGTH)
            header = text if call_step is not None else f"{step.name}: {text}"
            columns.append(QuestionColumn(step.pk, question, header))
    return columns


def _option_labels(question) -> dict[str, str]:
    labels = {}
    for option in question.question_options.all():
        labels[str(option.uuid)] = option.label
        labels[UUID(str(option.uuid)).hex] = option.label
    return labels


def _option_key(value) -> str:
    try:
        return UUID(str(value)).hex
    except (TypeError, ValueError, AttributeError):
        return str(value)


def answer_value(question, answer_data):
    """An answer as a cell: option labels for a selection, file names for
    files, Yes or No, a number where the question takes one, else its text.
    A structured value with no reading of its own is written as JSON rather
    than dropped, so nothing answered goes missing."""
    if answer_data in (None, "", []):
        return ""
    question_type = question.question_type
    if question_type in (QuestionTypes.SINGLE_SELECT, QuestionTypes.MULTI_SELECT):
        labels = _option_labels(question)
        values = answer_data if isinstance(answer_data, list) else [answer_data]
        return ", ".join(
            labels.get(str(value)) or labels.get(_option_key(value)) or str(value)
            for value in values
        )
    if question_type in (QuestionTypes.FILE, QuestionTypes.MULTIPLE_FILES):
        files = answer_data if isinstance(answer_data, list) else [answer_data]
        return ", ".join(
            str(item.get("name"))
            for item in files
            if isinstance(item, dict) and item.get("name")
        )
    if isinstance(answer_data, bool):
        return _("Yes") if answer_data else _("No")
    if isinstance(answer_data, (int, float, Decimal)):
        return answer_data
    if isinstance(answer_data, list):
        return ", ".join(
            json.dumps(item) if isinstance(item, (dict, list)) else str(item)
            for item in answer_data
        )
    if isinstance(answer_data, dict):
        return json.dumps(answer_data, sort_keys=True)
    return str(answer_data)


def assessment_steps(call) -> list[models.CallWorkflowStep]:
    """The steps whose outcome the panel sheet shows: the evaluation's
    assessments and decisions, enabled, in their order."""
    return list(
        models.CallWorkflowStep.objects.filter(
            call=call,
            is_enabled=True,
            kind__in=(StepKinds.ASSESSMENT, StepKinds.DECISION),
        )
        .exclude(phase__in=WorkflowPhases.POST_AWARD)
        .order_by("order", "id")
    )


@dataclass(frozen=True)
class OfferingAssessmentColumn:
    call_step_id: int
    requested_offering_id: int
    header: str


def offering_assessment_columns(call, steps) -> list[OfferingAssessmentColumn]:
    """A column per requested offering and per step assessing each offering
    apart (``per_requested_offering``): its technical assessment, say.

    Keyed on the call's entry for the offering, as its slots are: two
    entries of one offering -- a superseded request beside the accepted one
    -- are assessed apart. The entries are those an applicant could ask
    for, or that proposals asked for before they were cancelled -- the
    requested amounts' rule (``exports.CallExportSchema``).
    """
    per_offering = [step for step in steps if step.per_requested_offering]
    if not per_offering:
        return []
    used = set(
        models.RequestedResource.objects.filter(proposal__round__call=call)
        .values_list("requested_offering_id", flat=True)
        .distinct()
    )
    requested_offerings = [
        requested_offering
        for requested_offering in call.requestedoffering_set.select_related(
            "offering"
        ).order_by("offering__name", "id")
        if requested_offering.state == RequestedOfferingStates.ACCEPTED
        or requested_offering.id in used
    ]
    columns = []
    for step in per_offering:
        for requested_offering in requested_offerings:
            name = requested_offering.offering.name
            header = name if len(per_offering) == 1 else f"{name}: {step.name}"
            columns.append(
                OfferingAssessmentColumn(step.pk, requested_offering.pk, header)
            )
    return columns


# =============================================================================
# The evaluations export
# =============================================================================


def evaluation_proposals(
    call,
    viewer: Viewer,
    round_uuid=None,
    panel=None,
    proposal_uuids=None,
    proposal_name=None,
):
    """The proposals whose contributions the evaluations export covers."""
    queryset = models.Proposal.objects.filter(round__call=call)
    if round_uuid:
        queryset = queryset.filter(round__uuid=round_uuid)
    if panel is not None:
        queryset = queryset.filter(panel=panel)
    if proposal_uuids:
        queryset = queryset.filter(uuid__in=proposal_uuids)
    if proposal_name:
        queryset = queryset.filter(name__icontains=proposal_name)
    return viewer.proposals(queryset).order_by("round__cutoff_time", "slug", "id")


def _batches(proposal_ids, size=BATCH_SIZE):
    for start in range(0, len(proposal_ids), size):
        yield proposal_ids[start : start + size]


def _sort_key(contribution):
    """By proposal, step, and within a step by when the contributions were
    made -- in their anonymous order on a blind step, whose place in the list
    must tell no more than the label (``contributions.listing_order``)."""
    instance = contribution.proposal_step
    blind = instance.call_step.blind_review
    return (
        instance.order,
        instance.pk,
        contribution.uuid.hex if blind else "",
        contribution.created,
        contribution.pk,
    )


def _readable_batch(viewer: Viewer, batch, call_step=None) -> dict[int, list]:
    """The contributions to the proposals ``batch`` names that the reader
    may read, by proposal, their answers loaded."""
    queryset = models.StepContribution.objects.filter(
        proposal_step__proposal_id__in=batch
    ).select_related(
        "proposal_step__call_step__checklist",
        "proposal_step__proposal__round__call",
        "proposal_step__proposal__panel",
        "requested_offering__offering",
        "contributor",
        "delegate",
        "review__proposal__round",
    )
    if call_step is not None:
        queryset = queryset.filter(proposal_step__call_step=call_step)
    by_proposal: dict[int, list] = {}
    for contribution in queryset:
        if viewer.reads_contribution(contribution):
            by_proposal.setdefault(contribution.proposal_step.proposal_id, []).append(
                contribution
            )
    contributions.prefetch_answers([c for items in by_proposal.values() for c in items])
    return by_proposal


def evaluation_rows(viewer: Viewer, call, proposals, call_step=None) -> Iterator[list]:
    """One row per contribution the reader may read, proposal by proposal.

    For the call's managers the question columns are the call's (or the
    step's), so two exports of one call compare, and the rows stream a batch
    of proposals at a time. Anyone else exports one panel, and gets the
    columns of the steps they read a contribution of only: a question of a
    step they may not read stays out of their file, header included.
    """
    proposal_ids = list(proposals.values_list("pk", flat=True))
    viewer.take(proposal_ids)
    if viewer.manages:
        columns = question_columns(call, call_step)
        batches = (
            (batch, _readable_batch(viewer, batch, call_step))
            for batch in _batches(proposal_ids)
        )
    else:
        batches = [
            (batch, _readable_batch(viewer, batch, call_step))
            for batch in _batches(proposal_ids)
        ]
        read_step_ids = {
            contribution.proposal_step.call_step_id
            for _batch, by_proposal in batches
            for items in by_proposal.values()
            for contribution in items
        }
        columns = question_columns(call, call_step, step_ids=read_step_ids)
    yield unique_headers(
        [
            *(column.header for column in EVALUATION_COLUMNS),
            *(column.header for column in columns),
        ]
    )
    for batch, by_proposal in batches:
        for proposal_id in batch:
            for contribution in sorted(by_proposal.get(proposal_id, []), key=_sort_key):
                yield _evaluation_row(viewer, contribution, columns)


def _evaluation_row(viewer, contribution, columns) -> list:
    row = Row(
        proposal=contribution.proposal_step.proposal,
        contribution=contribution,
        viewer=viewer,
    )
    call_step = contribution.proposal_step.call_step
    answers = {}
    completion = contribution.get_checklist_completion(call_step.checklist)
    if completion is not None:
        answers = contributions.latest_answers(completion)
    return [
        *(column.value(row) for column in EVALUATION_COLUMNS),
        *(
            answer_value(column.question, answers[column.question.pk].answer_data)
            if column.call_step_id == call_step.pk and column.question.pk in answers
            else ""
            for column in columns
        ),
    ]


# =============================================================================
# The panel sheet
# =============================================================================


@dataclass
class PanelSheetSchema:
    """The columns of a call's panel sheets, resolved once per export: every
    panel's sheet of one call has the same shape."""

    call: models.Call
    proposals: exports.CallExportSchema = field(init=False)
    steps: list = field(init=False)
    offering_columns: list = field(init=False)

    def __post_init__(self):
        self.proposals = exports.CallExportSchema(self.call)
        self.steps = assessment_steps(self.call)
        self.offering_columns = offering_assessment_columns(self.call, self.steps)

    def header(self) -> list[str]:
        return unique_headers(self._header())

    def _header(self) -> list[str]:
        return [
            *(column.header for column in PANEL_SHEET_COLUMNS),
            *(header for _, _, header in self.proposals.component_columns),
            *exports.PROPOSAL_REVIEW_HEADERS,
            *(f"Average {header}" for _, header in self.proposals.criterion_columns),
            *(step.name for step in self.steps),
            *(
                header
                for column in self.offering_columns
                for header in (column.header, f"{column.header}: conditions")
            ),
        ]


def panel_proposals(call, viewer: Viewer, panel, round_uuid=None):
    """The proposals routed to ``panel`` that the reader may export."""
    queryset = models.Proposal.objects.filter(round__call=call, panel=panel)
    if round_uuid:
        queryset = queryset.filter(round__uuid=round_uuid)
    return (
        viewer.proposals(queryset)
        .select_related(
            "round__call",
            "panel",
            "created_by",
            "science_sub_domain",
            "current_step__call_step",
        )
        .prefetch_related(
            "requestedresource_set",
            "review_set",
            exports._active_steps_prefetch(),
            Prefetch(
                "workflow_step_instances",
                queryset=models.ProposalWorkflowStepInstance.objects.filter(
                    run__isnull=True
                )
                .select_related("call_step")
                .prefetch_related(
                    Prefetch(
                        "contributions",
                        queryset=models.StepContribution.objects.filter(
                            requested_offering__isnull=False
                        ).select_related(
                            "requested_offering", "contributor", "delegate"
                        ),
                        to_attr="offering_contributions",
                    )
                )
                .order_by("order", "id"),
                to_attr="evaluation_instances",
            ),
        )
        .order_by("round__cutoff_time", "slug", "id")
    )


def panel_sheet_rows(
    schema: PanelSheetSchema, viewer: Viewer, proposals
) -> Iterator[list]:
    yield schema.header()
    viewer.take(list(proposals.values_list("pk", flat=True)))
    for proposal in proposals.iterator(chunk_size=BATCH_SIZE):
        yield _panel_sheet_row(schema, viewer, proposal)


def _amounts(schema: exports.CallExportSchema, proposal) -> dict:
    amounts: dict[tuple[int, str], Decimal] = {}
    for requested_resource in proposal.requestedresource_set.all():
        offering_id = schema.offering_of_requested_offering.get(
            requested_resource.requested_offering_id
        )
        for component_type, amount in (requested_resource.limits or {}).items():
            if amount in (None, ""):
                continue
            key = (offering_id, component_type)
            try:
                amounts[key] = amounts.get(key, Decimal(0)) + Decimal(str(amount))
            except (TypeError, ValueError, ArithmeticError):
                continue
    return amounts


def _review_cells(schema: exports.CallExportSchema, viewer, proposal) -> list:
    width = len(exports.PROPOSAL_REVIEW_HEADERS) + len(schema.criterion_columns)
    if not viewer.reads_review_scores(proposal):
        return [""] * width
    reviews = [
        review
        for review in proposal.review_set.all()
        if review.state != models.Review.States.REJECTED
    ]
    submitted = [
        review for review in reviews if review.state == models.Review.States.SUBMITTED
    ]
    scores = [review.summary_score for review in submitted]
    return [
        len(reviews),
        len(submitted),
        round(sum(scores) / len(scores), 2) if scores else "",
        ", ".join(str(score) for score in sorted(scores)),
        *(
            exports._average(
                review.criteria_scores.get(key)
                for review in submitted
                if review.criteria_scores
            )
            for key, _header in schema.criterion_columns
        ),
    ]


def _step_cells(schema: PanelSheetSchema, viewer, instances) -> list:
    """The outcome of each assessment step, its status while it has none."""
    latest = {}
    for instance in instances:
        latest[instance.call_step_id] = instance
    cells = []
    for step in schema.steps:
        instance = latest.get(step.pk)
        if instance is None:
            cells.append("")
        elif instance.is_allocation_decision and viewer.decision_withheld(
            instance.proposal
        ):
            # As the step list shows it: still being decided.
            cells.append(STEP_STATUS_LABELS[WorkflowStepInstanceStatuses.ACTIVE])
        elif instance.outcome and viewer.reads_step_outcome(instance):
            cells.append(outcome_label(instance.outcome))
        else:
            cells.append(STEP_STATUS_LABELS.get(instance.status, instance.status))
    return cells


def _offering_cells(schema: PanelSheetSchema, viewer, instances) -> list:
    """The outcome and conditions of each offering's slot of each step that
    assesses offerings apart, where the reader may read the slot."""
    slots = {}
    for instance in instances:
        for contribution in instance.offering_contributions:
            key = (instance.call_step_id, contribution.requested_offering_id)
            slots[key] = contribution
    cells = []
    for column in schema.offering_columns:
        contribution = slots.get((column.call_step_id, column.requested_offering_id))
        if contribution is None or not viewer.reads_contribution(contribution):
            cells.extend(["", ""])
            continue
        outcome = outcome_label(contribution.outcome) or CONTRIBUTION_STATUS_LABELS.get(
            contribution.status, contribution.status
        )
        cells.extend([outcome, contribution.conditions])
    return cells


def _panel_sheet_row(schema: PanelSheetSchema, viewer, proposal) -> list:
    row = Row(proposal=proposal, viewer=viewer, extra={"schema": schema.proposals})
    amounts = _amounts(schema.proposals, proposal)
    instances = proposal.evaluation_instances
    return [
        *(column.value(row) for column in PANEL_SHEET_COLUMNS),
        *(
            exports._format_amount(amounts.get((offering_id, component_type)))
            for offering_id, component_type, _header in (
                schema.proposals.component_columns
            )
        ),
        *_review_cells(schema.proposals, viewer, proposal),
        *_step_cells(schema, viewer, instances),
        *_offering_cells(schema, viewer, instances),
    ]
