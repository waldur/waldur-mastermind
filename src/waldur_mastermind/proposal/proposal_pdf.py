"""A proposal as a PDF, for whoever may read it.

The document says nothing its reader could not read through the API: every
section is built from what the endpoint serving it answers them -- the
proposal, its requested resources, team, workflow steps, contributions,
per-offering assessments, released reports, ranking and award -- and a
section the endpoint refuses them is left out. So the applicant team gets
their application and what has been released to them, the call team the
evaluation record, and an evaluator what they evaluate, with no rule of the
document's own deciding which (``ProposalViewSet.pdf`` gathers the reads).

Rendered with Typst in a child process (``core.pdf.render_pdf_isolated``),
since the proposal's text is the applicant's and its length theirs to choose.
Every value reaches the template as a string already formatted and translated
here, and is inserted as text, never as markup.
"""

import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.utils import formats, timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.html import strip_tags
from django.utils.translation import gettext as _

from waldur_core.core import pdf
from waldur_mastermind.common.serializers import get_hidden_options
from waldur_mastermind.proposal.enums import (
    ContributionOutcomes,
    ProposalStates,
    StepContributionStatuses,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)

TEMPLATE = Path(__file__).parent / "pdf" / "proposal.typ"

# The subscription period is shown as the request's Period, not as an option.
PREPAID_DURATION_MONTHS = "prepaid_duration_months"

_LABELS = {
    "state": dict(ProposalStates.CHOICES),
    "status": dict(WorkflowStepInstanceStatuses.CHOICES),
    "outcome": {
        **dict(WorkflowStepOutcomes.CHOICES),
        **dict(ContributionOutcomes.CHOICES),
    },
    "contribution": dict(StepContributionStatuses.CHOICES),
}

EMPTY = "—"

# Ends of blocks and lines become spaces before the tags go, so
# "<p>Line one</p><p>Line two</p>" reads "Line one Line two".
_BLOCK_BOUNDARY = re.compile(r"</(p|div|li|h[1-6])\s*>|<br\s*/?>", re.IGNORECASE)


@dataclass
class ProposalReads:
    """What each endpoint answered the reader; None where it refused them."""

    proposal: dict
    resources: list | None = None
    team: list | None = None
    steps: list | None = None
    timeline: dict | None = None
    contributions: list | None = None
    assessments: list | None = None
    reports: list | None = None
    rankings: list | None = None
    award: list | None = None
    # Role name -> its description ("PROPOSAL.MEMBER" -> "Proposal member").
    role_labels: dict = field(default_factory=dict)
    # Completer UUID -> name, for those who read the timeline, which names
    # who completed each step.
    completer_names: dict = field(default_factory=dict)


def uuid_key(value) -> str:
    """A UUID as 32 hex characters, however it came: a serializer's data
    holds UUID objects until it is rendered, an API response strings."""
    if not value:
        return ""
    try:
        return uuid.UUID(str(value)).hex
    except ValueError:
        return str(value)


def _label(kind, value) -> str:
    if value in (None, ""):
        return ""
    label = _LABELS[kind].get(value)
    return _(label) if label else str(value).replace("_", " ").capitalize()


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _or_empty(value) -> str:
    return _text(value) or EMPTY


def _datetime(value) -> str:
    if not value:
        return ""
    parsed = parse_datetime(value) if isinstance(value, str) else value
    if parsed is None:
        return str(value)
    if timezone.is_aware(parsed):
        parsed = timezone.localtime(parsed)
    return formats.date_format(parsed, "DATETIME_FORMAT")


def _date(value) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        parsed = parse_datetime(value)
        value = parsed.date() if parsed else parse_date(value)
        if value is None:
            return ""
    return formats.date_format(value, "DATE_FORMAT")


def _amount(value) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return _text(value)
    if number == number.to_integral_value():
        return formats.number_format(int(number), force_grouping=True)
    return formats.number_format(number.normalize(), force_grouping=True)


def _months(value) -> str:
    try:
        months = int(value)
    except (TypeError, ValueError):
        return ""
    if months <= 0:
        return ""
    return _("1 month") if months == 1 else _("%(months)s months") % {"months": months}


# Requested resources ---------------------------------------------------------


def _option_value(option: dict, value) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, bool):
        return _("Yes") if value else _("No")
    if isinstance(value, list | tuple):
        return ", ".join(_option_value(option, item) for item in value if item)
    if isinstance(value, dict):
        # OpenStack selectors and older records keep the chosen object.
        picked = value.get("backend_id") or value.get("value") or value.get("name")
        return _text(picked) if picked else ""
    if option.get("type") == "html_text":
        spaced = _BLOCK_BOUNDARY.sub(lambda match: match.group(0) + " ", str(value))
        return " ".join(strip_tags(spaced).split())
    if option.get("type") == "date":
        return _date(value) or _text(value)
    return _text(value)


def option_summary(offering_options, attributes) -> list[str]:
    """The options a request answers, in the offering's order, as
    "Label: value" -- as the requested resources table shows them. Options
    hidden by their visible_if rules and those left empty are not listed."""
    options = (offering_options or {}).get("options") or {}
    if not options or not isinstance(attributes, dict):
        return []
    hidden = get_hidden_options(options, attributes)
    order = (offering_options or {}).get("order") or list(options)
    lines = []
    for key in order:
        option = options.get(key)
        if not option or key in hidden or key == PREPAID_DURATION_MONTHS:
            continue
        value = _option_value(option, attributes.get(key))
        if value:
            lines.append(f"{option.get('label') or key}: {value}")
    return lines


def _component_lines(requested_offering: dict, limits) -> list[str]:
    components = {
        component.get("type"): component
        for component in requested_offering.get("components") or []
    }
    lines = []
    for component_type, amount in (limits or {}).items():
        if amount is None:
            continue
        component = components.get(component_type) or {}
        unit = component.get("measured_unit")
        name = component.get("name") or component_type
        lines.append(f"{name}: {_amount(amount)}" + (f" {unit}" if unit else ""))
    return lines


def _period(requested_offering: dict, attributes) -> str:
    prepaid = any(
        component.get("is_prepaid")
        for component in requested_offering.get("components") or []
    )
    if not prepaid or not isinstance(attributes, dict):
        return ""
    months = _months(attributes.get(PREPAID_DURATION_MONTHS))
    if months:
        return months
    # Requests written before the switch to a length name an end date.
    end_date = _date(attributes.get("end_date"))
    return _("until %(date)s") % {"date": end_date} if end_date else ""


def _resource_row(item: dict) -> list[list[str]]:
    offering = item.get("requested_offering") or {}
    requested = _component_lines(offering, item.get("limits")) + option_summary(
        offering.get("options"), item.get("attributes")
    )
    return [
        [_or_empty(offering.get("offering_name") or offering.get("name"))],
        requested or [EMPTY],
        [_period(offering, item.get("attributes")) or EMPTY],
        [_or_empty(item.get("purchase_order_reference"))],
    ]


def _file_name(url_or_name) -> str:
    name = _text(url_or_name)
    if not name:
        return ""
    return name.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]


# Sections --------------------------------------------------------------------


def _facts(heading: str, rows: list[tuple[str, str]]) -> dict:
    return {
        "kind": "facts",
        "heading": heading,
        "facts": [{"label": label, "value": value} for label, value in rows if value],
    }


def _table(heading, columns, rows, empty, note="", widths=None) -> dict:
    """``widths`` per column: "auto", a share of the page ("30%") or of what
    the others leave ("1fr"). Without them the last column takes the rest."""
    if not rows:
        return {"kind": "empty", "heading": heading, "note": note or empty}
    section = {
        "kind": "table",
        "heading": heading,
        "note": note,
        "columns": columns,
        "rows": rows,
    }
    if widths:
        section["widths"] = widths
    return section


def _application(proposal: dict) -> list[dict]:
    round_ = proposal.get("round") or {}
    sections = [
        _facts(
            _("Application"),
            [
                (_("Proposal ID"), _text(proposal.get("slug"))),
                (_("Call"), _text(proposal.get("call_name"))),
                (_("Round"), _text(round_.get("name"))),
                (_("State"), _label("state", proposal.get("state"))),
                (_("Applicant"), _text(proposal.get("created_by_name"))),
                (_("Email"), _text(proposal.get("applicant_email"))),
                (_("Organization"), _text(proposal.get("applicant_organization"))),
                (_("Science domain"), _text(proposal.get("science_domain_name"))),
                (
                    _("Science sub-domain"),
                    _text(proposal.get("science_sub_domain_name")),
                ),
                (_("Panel"), _text(proposal.get("panel_name"))),
                (
                    _("Requested duration"),
                    _months(proposal.get("requested_duration_months")),
                ),
                (_("Created"), _datetime(proposal.get("created"))),
                (_("Submitted"), _datetime(proposal.get("submitted_at"))),
            ],
        )
    ]
    for heading, key in (
        (_("Summary"), "project_summary"),
        (_("Description"), "description"),
    ):
        body = _text(proposal.get(key))
        if body:
            sections.append({"kind": "text", "heading": heading, "body": body})
    return sections


def _resources(proposal: dict, resources: list) -> dict:
    note = ""
    if proposal.get("reads_own_offerings_only"):
        note = _(
            "Only your offerings' part is shown. What is requested of other "
            "providers' offerings is not shown to you."
        )
    return _table(
        _("Requested resources"),
        [_("Offering"), _("Requested"), _("Period"), _("Purchase order")],
        [_resource_row(item) for item in resources],
        _("No resources requested."),
        note,
        widths=["25%", "1fr", "auto", "auto"],
    )


def _team(team: list, role_labels: dict) -> dict:
    rows = [
        [
            [_or_empty(member.get("user_full_name") or member.get("user_username"))],
            [_or_empty(member.get("user_email"))],
            [
                _or_empty(
                    role_labels.get(member.get("role_name")) or member.get("role_name")
                )
            ],
        ]
        for member in team
    ]
    return _table(
        _("Project team"),
        [_("Name"), _("Email"), _("Role")],
        rows,
        _("No team members."),
    )


def _attachments(proposal: dict, resources: list | None) -> dict:
    names = [
        _file_name(document.get("file_name") or document.get("file"))
        for document in proposal.get("supporting_documentation") or []
    ]
    # A purchase order is an attachment too: named, not handed over.
    names += [_file_name(item.get("attachment")) for item in resources or []]
    names = [name for name in names if name]
    if not names:
        return {
            "kind": "empty",
            "heading": _("Attachments"),
            "note": _("No attachments."),
        }
    return {"kind": "list", "heading": _("Attachments"), "items": names}


def _steps(steps: list, timeline: dict | None, completer_names: dict) -> dict:
    # The timeline names who completed each step; only the call team reads
    # it. The name is the step instance's own completer: a step a post-award
    # run repeats has one per run.
    with_completer = timeline is not None
    rows = []
    for step in steps:
        outcome = [_label("outcome", step.get("outcome"))]
        for key in ("outcome_reason", "rejection_reason", "skipped_reason"):
            reason = _text(step.get(key))
            if reason:
                outcome.append(reason)
        notes = _text(step.get("internal_notes"))
        if notes:
            outcome.append(_("Internal notes: %(notes)s") % {"notes": notes})
        dates = [
            _datetime(step.get("started_at")),
            _datetime(step.get("completed_at")),
        ]
        row = [
            [_or_empty(step.get("step_name"))],
            [_label("status", step.get("status")) or EMPTY],
            [line for line in outcome if line] or [EMPTY],
            # Started, then completed, on lines of their own.
            [date for date in dates if date] or [EMPTY],
        ]
        if with_completer:
            row.append(
                [_or_empty(completer_names.get(uuid_key(step.get("completed_by"))))]
            )
        rows.append(row)
    columns = [_("Step"), _("Status"), _("Outcome"), _("Started, completed")]
    if with_completer:
        columns.append(_("Completed by"))
    # The step's name wraps within its share; its outcome takes the rest.
    widths = ["28%", "auto", "1fr", "auto"] + (["auto"] if with_completer else [])
    return _table(
        _("Evaluation"), columns, rows, _("No workflow steps yet."), widths=widths
    )


def _contribution_entry(item: dict) -> dict:
    who = _text(item.get("offering_name")) or _text(
        item.get("contributor_full_name") or item.get("user_full_name")
    )
    title = _text(item.get("step_name"))
    if who:
        title = f"{title} — {who}" if title else who
    lines = []
    status = _label("contribution", item.get("status"))
    outcome = _label("outcome", item.get("outcome"))
    meta = " · ".join(part for part in (outcome, status) if part)
    submitted = _datetime(item.get("submitted_at"))
    if submitted:
        meta = " · ".join(part for part in (meta, submitted) if part)
    conditions = _text(item.get("conditions"))
    if conditions:
        lines.append(_("Conditions: %(conditions)s") % {"conditions": conditions})
    comment = _text(item.get("comment"))
    if comment:
        lines.append(comment)
    for answer in item.get("answers") or []:
        value = _text(answer.get("answer_display"))
        question = _text(answer.get("question_description"))
        if value and question:
            lines.append(f"{question}: {value}")
    return {"title": title or EMPTY, "meta": meta, "lines": lines}


def _contributions(items: list) -> dict | None:
    if not items:
        return None
    return {
        "kind": "entries",
        "heading": _("Contributions"),
        "entries": [_contribution_entry(item) for item in items],
    }


def _assessments(items: list) -> dict | None:
    if not items:
        return None
    rows = [
        [
            [_or_empty(item.get("offering_name"))],
            [_or_empty(item.get("step_name"))],
            [
                _label("outcome", item.get("outcome"))
                or _label("contribution", item.get("status"))
                or EMPTY
            ],
            [
                line
                for line in (_text(item.get("conditions")), _text(item.get("comment")))
                if line
            ]
            or [EMPTY],
        ]
        for item in items
    ]
    return _table(
        _("Technical assessment per offering"),
        [_("Offering"), _("Step"), _("Outcome"), _("Conditions and comment")],
        rows,
        "",
    )


def _reports(reports: list) -> dict | None:
    if not reports:
        return None
    entries = []
    for report in reports:
        meta = _datetime(report.get("released_at"))
        released_by = _text(report.get("released_by_name"))
        if released_by:
            meta = _("%(date)s by %(name)s") % {"date": meta, "name": released_by}
        lines = []
        text = _text(report.get("text"))
        if text:
            lines.append(text)
        for contribution in report.get("contributions") or []:
            entry = _contribution_entry(contribution)
            lines.append(
                entry["title"] + (f" ({entry['meta']})" if entry["meta"] else "")
            )
            lines += entry["lines"]
        entries.append(
            {
                "title": _or_empty(report.get("step_name")),
                "meta": _("Released %(when)s") % {"when": meta} if meta else "",
                "lines": lines,
            }
        )
    return {"kind": "entries", "heading": _("Released reports"), "entries": entries}


def _rankings(rankings: list) -> dict | None:
    rows = []
    for ranking in rankings:
        # "Offering: Core hours 2 400 000 h, Storage 10 TB", one per offering.
        recommended = []
        for offering in ranking.get("recommended_resources_detail") or []:
            amounts = ", ".join(
                f"{_text(component.get('name'))} {_amount(component.get('amount'))}"
                + (
                    f" {component['measured_unit']}"
                    if component.get("measured_unit")
                    else ""
                )
                for component in offering.get("components") or []
                if component.get("amount") is not None
            )
            name = _text(offering.get("offering_name"))
            recommended.append(f"{name}: {amounts}" if amounts else name)
        final_score = ranking.get("final_score")
        rows.append(
            [
                [
                    _or_empty(
                        ranking.get("meeting_note_title") or ranking.get("round_name")
                    )
                ],
                [_or_empty(ranking.get("rank"))],
                [_amount(final_score) if final_score is not None else EMPTY],
                [line for line in recommended if line] or [EMPTY],
                [_or_empty(ranking.get("notes"))],
            ]
        )
    if not rows:
        return None
    return _table(
        _("Ranking"),
        [
            _("Ranking"),
            _("Rank"),
            _("Final score"),
            _("Recommended resources"),
            _("Notes"),
        ],
        rows,
        "",
    )


def _award(proposal: dict, award: list) -> dict | None:
    if not award:
        return None
    rows = []
    for item in award:
        offering = item.get("requested_offering") or {}
        lines = _component_lines(offering, item.get("limits")) + option_summary(
            offering.get("options"), item.get("attributes")
        )
        rows.append(
            [
                [_or_empty(offering.get("offering_name") or offering.get("name"))],
                [_or_empty(item.get("plan_name"))],
                lines or [EMPTY],
            ]
        )
    duration = _months(proposal.get("awarded_duration_months"))
    note = _("Awarded for %(duration)s.") % {"duration": duration} if duration else ""
    return _table(_("Award"), [_("Offering"), _("Plan"), _("Awarded")], rows, "", note)


def build_document(reads: ProposalReads) -> dict:
    proposal = reads.proposal
    sections = _application(proposal)
    if reads.resources is not None:
        sections.append(_resources(proposal, reads.resources))
    if reads.team is not None:
        sections.append(_team(reads.team, reads.role_labels))
    sections.append(_attachments(proposal, reads.resources))
    if reads.steps is not None:
        sections.append(_steps(reads.steps, reads.timeline, reads.completer_names))
    for section in (
        _assessments(reads.assessments or []),
        _contributions(reads.contributions or []),
        _reports(reads.reports or []),
        _rankings(reads.rankings or []),
        _award(proposal, reads.award or []),
    ):
        if section:
            sections.append(section)

    name = _text(proposal.get("name")) or _text(proposal.get("slug"))
    round_ = proposal.get("round") or {}
    reference = _text(proposal.get("slug")) or name
    return {
        "title": name,
        "subtitle": " · ".join(
            part
            for part in (
                _text(proposal.get("slug")),
                _text(proposal.get("call_name")),
                _text(round_.get("name")),
            )
            if part
        ),
        "footer": _("%(reference)s — exported %(date)s")
        % {"reference": reference, "date": _date(timezone.now())},
        "sections": sections,
    }


def file_name(proposal: dict) -> str:
    base = _text(proposal.get("slug")) or _text(proposal.get("uuid")) or "proposal"
    # Path separators and characters Windows refuses never reach the name.
    safe = "".join(
        char if char.isalnum() or char in "-_." else "-" for char in base
    ).strip("-.")
    return f"{safe or 'proposal'}.pdf"


def render_proposal_pdf(reads: ProposalReads) -> bytes:
    return pdf.render_pdf_isolated(TEMPLATE, build_document(reads))
