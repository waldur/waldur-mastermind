"""CSV exports of a call's proposals and reviews.

Panel meetings, funding-body reporting and archiving all work from a
spreadsheet of proposals. The table export in the frontend can only write the
columns a list happens to show, and the figures a panel actually needs — the
amount requested per offering component, the current workflow step, the review
scores — are not among them. Paging a large call through the API to collect
them is slow besides, so the call writes the file itself.

The column set is derived from the *call*, not from the proposals that happen
to match the filters, so every row of one call's export has the same shape and
two exports of the same call are comparable.
"""

import csv
from collections import Counter
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation

from django.db.models import Prefetch
from django.http import StreamingHttpResponse

from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.proposal import contributions, models, utils
from waldur_mastermind.proposal.enums import (
    RequestedOfferingStates,
    WorkflowStepInstanceStatuses,
)


class Echo:
    """Write-only file-like object that hands each written row straight back.

    ``csv.writer`` needs something with ``write``; a streaming response needs
    an iterator of strings. This is the adapter between the two.
    """

    def write(self, value):
        return value


# Characters that make a spreadsheet read a cell as a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _is_number(value: str) -> bool:
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def _safe_cell(value):
    """Neutralise text a spreadsheet would run as a formula.

    Proposal names, applicant profiles and review comments are written by
    applicants and reviewers, and the file is opened by call managers — a
    name like ``=HYPERLINK(...)`` would otherwise execute on their machine.
    A leading apostrophe makes Excel and LibreOffice show the text as typed.
    Numbers pass through, so a negative amount stays a number.
    """
    if (
        isinstance(value, str)
        and value.startswith(FORMULA_PREFIXES)
        and not _is_number(value)
    ):
        return f"'{value}"
    return value


def _text(value) -> str:
    return "" if value is None else str(value)


def _timestamp(value) -> str:
    return value.isoformat() if value else ""


def _active_steps_prefetch(prefix=""):
    """The proposals' active steps, for ``_step_label``."""
    return Prefetch(
        f"{prefix}workflow_step_instances",
        queryset=models.ProposalWorkflowStepInstance.objects.filter(
            status=WorkflowStepInstanceStatuses.ACTIVE
        )
        .select_related("call_step")
        .order_by("order", "id"),
        to_attr="active_step_instances",
    )


def _step_label(proposal) -> str:
    """Name of the call step the proposal stands in, blank outside the workflow;
    the names of all of them while several of a stage run at once."""
    if proposal.current_step_id is None:
        return ""
    active = getattr(proposal, "active_step_instances", None) or []
    if len(active) > 1:
        return " · ".join(i.call_step.name or i.step for i in active)
    return proposal.current_step.call_step.name


def _is_requestable(component) -> bool:
    """Components an applicant can name an amount for.

    The same set :meth:`Proposal.offerings_missing_requested_amounts` checks:
    a limit component, or a one-time component bought by the month.
    """
    if component.billing_type == BillingTypes.LIMIT:
        return True
    return component.billing_type == BillingTypes.ONE_TIME and component.is_prepaid


def criterion_columns(call: models.Call) -> list[tuple[str, str]]:
    """``(criterion key, header)`` for every criterion of the call's steps.

    A criterion is headed by its name, and by its step's name as well where
    another step of the call has a criterion of the same name.
    """
    criteria = list(
        models.WorkflowCriterion.objects.filter(workflow_step__call=call)
        .select_related("workflow_step")
        .order_by("workflow_step__order", "workflow_step__id", "order", "id")
    )
    repeated = {
        name
        for name, count in Counter(criterion.name for criterion in criteria).items()
        if count > 1
    }
    return [
        (
            contributions.criterion_key(criterion),
            f"{criterion.workflow_step.name}: {criterion.name}"
            if criterion.name in repeated
            else criterion.name,
        )
        for criterion in criteria
    ]


class CallExportSchema:
    """The columns one call's proposal export uses, resolved once per export.

    Walking the call's requested offerings here rather than per proposal keeps
    the export to a fixed number of queries no matter how many proposals it
    covers.
    """

    def __init__(self, call: models.Call):
        self.call = call
        # (offering_id, component_type, header). Keyed on the offering rather
        # than on the call's entry for it: a call may hold more than one entry
        # for the same offering (a superseded request beside the accepted one),
        # and a panel wants one column per thing asked for, not one per row of
        # the call's configuration.
        self.component_columns: list[tuple[int, str, str]] = []
        self.offering_of_requested_offering: dict[int, int] = {}
        # Requested offerings bought by the month, which is what the project's
        # length is derived from when the call does not fix one.
        self.prepaid_requested_offering_ids: set[int] = set()
        self.criterion_columns = criterion_columns(call)

        # An offering gets columns once it can carry amounts: when it is
        # accepted, or when proposals already asked for it before it was
        # cancelled. A pending offering cannot be requested against yet, so it
        # would only add columns that are empty on every row. Read across the
        # whole call, not the filtered proposals, so the shape stays stable.
        used_requested_offering_ids = set(
            models.RequestedResource.objects.filter(proposal__round__call=call)
            .values_list("requested_offering_id", flat=True)
            .distinct()
        )
        requested_offerings = (
            call.requestedoffering_set.select_related("offering")
            .prefetch_related("offering__components")
            .order_by("offering__name", "id")
        )
        seen: set[tuple[int, str]] = set()
        for requested_offering in requested_offerings:
            offering = requested_offering.offering
            self.offering_of_requested_offering[requested_offering.id] = offering.id
            components = sorted(
                offering.components.all(), key=lambda c: (c.name, c.type)
            )
            if requested_offering.state == RequestedOfferingStates.ACCEPTED and any(
                component.is_prepaid for component in components
            ):
                self.prepaid_requested_offering_ids.add(requested_offering.id)
            if (
                requested_offering.state != RequestedOfferingStates.ACCEPTED
                and requested_offering.id not in used_requested_offering_ids
            ):
                continue
            for component in components:
                if not _is_requestable(component):
                    continue
                key = (offering.id, component.type)
                if key in seen:
                    continue
                seen.add(key)
                header = f"{offering.name} / {component.name}"
                if component.measured_unit:
                    header = f"{header} ({component.measured_unit})"
                self.component_columns.append((*key, header))

    def duration_label(self, proposal: models.Proposal) -> str:
        """The project length, unit included, as the call resolves it.

        Mirrors :func:`proposal.utils.requested_duration_label` minus its
        already-allocated branch, which needs a query per proposal. Months and
        days are never converted into each other — a day count is true only
        relative to the date it was measured from.
        """
        fixed_days = self.call.fixed_duration_in_days
        if fixed_days:
            return f"{fixed_days} days"

        if proposal.requested_duration_months:
            months = proposal.requested_duration_months
            return "1 month" if months == 1 else f"{months} months"

        lengths = [
            months
            for requested_resource in proposal.requestedresource_set.all()
            if requested_resource.requested_offering_id
            in self.prepaid_requested_offering_ids
            and (months := utils.requested_months(requested_resource)) is not None
        ]
        if not lengths:
            return ""
        months = max(lengths)
        return "1 month" if months == 1 else f"{months} months"


PROPOSAL_HEADERS = [
    "Proposal ID",
    "Name",
    "Round",
    "Applicant",
    "Applicant email",
    "Organisation",
    "State",
    "Workflow step",
    "Created",
    "Submitted",
    "Science sub-domain",
    # What the proposal asks for. What was granted is not read back here — it
    # needs a query per proposal — so the header must not claim more.
    "Requested duration",
]

PROPOSAL_REVIEW_HEADERS = [
    "Reviews assigned",
    "Reviews submitted",
    "Average score",
    "Scores",
]

REVIEW_HEADERS = [
    "Proposal ID",
    "Proposal",
    "Round",
    "Proposal step",
    "Reviewer",
    "Reviewer email",
    "State",
    "Score",
    "Public comment",
    "Review due",
    "Created",
]


def proposal_queryset(
    call,
    round_uuid=None,
    states=None,
    applicant_uuid=None,
    name=None,
    proposal_uuids=None,
):
    """The proposals the export covers.

    Mirrors the filters the proposal list offers, so a manager who filters the
    table and exports it gets the rows they were looking at. The list's
    organisation filter has no counterpart: every proposal of one call shares
    its managing organisation, so it cannot discriminate here.
    """
    queryset = models.Proposal.objects.filter(round__call=call)
    if round_uuid:
        queryset = queryset.filter(round__uuid=round_uuid)
    if states:
        queryset = queryset.filter(state__in=states)
    if applicant_uuid:
        queryset = queryset.filter(created_by__uuid=applicant_uuid)
    if proposal_uuids:
        queryset = queryset.filter(uuid__in=proposal_uuids)
    if name:
        queryset = queryset.filter(name__icontains=name)
    return (
        queryset.select_related(
            "round",
            "created_by",
            "science_sub_domain",
            "current_step__call_step",
        )
        .prefetch_related(
            "requestedresource_set", "review_set", _active_steps_prefetch()
        )
        .order_by("round__cutoff_time", "slug", "id")
    )


def review_queryset(
    call,
    round_uuid=None,
    states=None,
    reviewer_uuid=None,
    proposal_uuids=None,
    proposal_name=None,
):
    """The reviews the export covers — the review list's filters, as above."""
    queryset = models.Review.objects.filter(proposal__round__call=call)
    if round_uuid:
        queryset = queryset.filter(proposal__round__uuid=round_uuid)
    if states:
        queryset = queryset.filter(state__in=states)
    if reviewer_uuid:
        queryset = queryset.filter(reviewer__uuid=reviewer_uuid)
    if proposal_uuids:
        queryset = queryset.filter(proposal__uuid__in=proposal_uuids)
    if proposal_name:
        queryset = queryset.filter(proposal__name__icontains=proposal_name)
    return (
        queryset.select_related(
            "proposal",
            "proposal__round",
            "proposal__current_step__call_step",
            "reviewer",
        )
        .prefetch_related(_active_steps_prefetch("proposal__"))
        .order_by("proposal__slug", "created", "id")
    )


def proposal_rows(schema: CallExportSchema, queryset) -> Iterator[list[str]]:
    yield [
        *PROPOSAL_HEADERS,
        *(header for _, _, header in schema.component_columns),
        *PROPOSAL_REVIEW_HEADERS,
        *(f"Average {header}" for _, header in schema.criterion_columns),
    ]
    for proposal in queryset.iterator(chunk_size=100):
        # Amounts are summed: one proposal may hold several requests against
        # the same offering, and the panel reads the total it was asked for.
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
                except (TypeError, ValueError, InvalidOperation):
                    continue

        # Rejected reviews were declined, expired or dropped for a conflict
        # of interest; counting them would read as reviews still outstanding.
        reviews = [
            review
            for review in proposal.review_set.all()
            if review.state != models.Review.States.REJECTED
        ]
        submitted = [
            review
            for review in reviews
            if review.state == models.Review.States.SUBMITTED
        ]
        scores = [review.summary_score for review in submitted]

        yield [
            proposal.slug,
            proposal.name,
            proposal.round.slug or proposal.round.name,
            _text(proposal.created_by and proposal.created_by.full_name),
            _text(proposal.created_by and proposal.created_by.email),
            _text(proposal.created_by and proposal.created_by.organization),
            proposal.get_state_display(),
            _step_label(proposal),
            _timestamp(proposal.created),
            # Empty for proposals submitted before the field existed — see its
            # help_text. Never guessed.
            _timestamp(proposal.submitted_at),
            _text(proposal.science_sub_domain and proposal.science_sub_domain.name),
            schema.duration_label(proposal),
            *(
                _format_amount(amounts.get((offering_id, component_type)))
                for offering_id, component_type, _header in schema.component_columns
            ),
            len(reviews),
            len(submitted),
            round(sum(scores) / len(scores), 2) if scores else "",
            # Sorted rather than in review order: the column is a distribution,
            # not a sequence, and nothing in the row says whose score is whose.
            ", ".join(str(score) for score in sorted(scores)),
            *(
                _average(
                    review.criteria_scores.get(key)
                    for review in submitted
                    if review.criteria_scores
                )
                for key, _header in schema.criterion_columns
            ),
        ]


def _average(values) -> float | str:
    present = [value for value in values if value is not None]
    return round(sum(present) / len(present), 2) if present else ""


def _format_amount(amount: Decimal | None) -> str:
    """Whole numbers without a trailing ``.0``; limits are usually integers."""
    if amount is None:
        return ""
    return str(
        amount.to_integral_value() if amount == amount.to_integral_value() else amount
    )


def review_rows(criteria: list[tuple[str, str]], queryset) -> Iterator[list[str]]:
    """One row per review; ``criteria`` are the call's ``criterion_columns``."""
    score_at = REVIEW_HEADERS.index("Score") + 1
    yield [
        *REVIEW_HEADERS[:score_at],
        *(header for _, header in criteria),
        *REVIEW_HEADERS[score_at:],
    ]
    for review in queryset.iterator(chunk_size=100):
        proposal = review.proposal
        submitted = review.state == models.Review.States.SUBMITTED
        row = [
            proposal.slug,
            proposal.name,
            proposal.round.slug or proposal.round.name,
            _step_label(proposal),
            _text(review.reviewer and review.reviewer.full_name),
            _text(review.reviewer and review.reviewer.email),
            review.get_state_display(),
            # A score is only meaningful once the review is in: the field
            # defaults to 0, and reporting that as a verdict would be a lie.
            review.summary_score if submitted else "",
            review.summary_public_comment,
            _timestamp(review.review_end_date),
            _timestamp(review.created),
        ]
        # The scores of the criteria of the review's step; another step's
        # criteria stay empty on its row.
        row[score_at:score_at] = [
            (review.criteria_scores or {}).get(key, "") if submitted else ""
            for key, _header in criteria
        ]
        yield row


def csv_response(rows: Iterator[list[str]], filename: str) -> StreamingHttpResponse:
    """Stream rows as a CSV attachment.

    Streamed rather than assembled: a call with thousands of proposals would
    otherwise hold the whole file in memory and send nothing until it was
    finished, which is what puts a request past a proxy's read timeout.
    """
    writer = csv.writer(Echo())
    response = StreamingHttpResponse(
        (writer.writerow([_safe_cell(cell) for cell in row]) for row in rows),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
