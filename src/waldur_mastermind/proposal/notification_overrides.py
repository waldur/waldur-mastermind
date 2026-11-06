"""A call's own wording of the mail it sends.

A call manager may replace, for one call, the subject, text body and HTML
body of each mail the call sends (``CallNotificationOverride``). The mail
is resolved, as it is sent, in this order: the call's own wording, then the
portal's (a staff override of the template, ``NotificationTemplate``, read
by the template loader), then the template shipped with Waldur.

An override changes wording only. It is rendered with the very context the
recipient's mail is rendered with -- already masked for that recipient: the
applicant's mail carries no outcome, no reviewer and no evaluator -- and
only with the variables documented for its mail type. It is rendered in a
sandbox:

- a template engine of its own, with no loaders (no ``include``, no
  ``extends``, no template of the portal is reachable) and a library of
  allowed tags and filters only: no ``load``, ``url``, ``debug``,
  ``csrf_token``, ``autoescape``, ``safe``, nor anything able to read
  settings or make output of unbounded size;
- the context is reduced to plain values (text, numbers, dates, lists and
  mappings of them), so nothing it holds has a method or an attribute to
  reach; Django refuses names starting with an underscore already;
- the HTML body is autoescaped, the subject and text are not, as the
  shipped templates are rendered;
- sources, loop nesting and output are bounded in size.

Saving validates an override by rendering it against an example context of
its mail type; a mail whose override fails to render at sending time is sent
with the portal's wording instead, and the failure is logged.
"""

import dataclasses
import datetime
import decimal
import logging

from django.template import (
    Context,
    Engine,
    Library,
    Template,
    TemplateSyntaxError,
    defaultfilters,
    defaulttags,
)
from django.template.defaulttags import ForNode
from django.template.loader import get_template
from django.utils import timezone

from waldur_core.configuration import config
from waldur_core.core import utils as core_utils
from waldur_core.core.models import NotificationTemplate
from waldur_core.core.service_access import names_calls
from waldur_mastermind.proposal.models import CallNotificationOverride

logger = logging.getLogger(__name__)

APP = "proposal"

MAX_SUBJECT = 2_000
MAX_BODY_TEXT = 20_000
MAX_BODY_HTML = 50_000
MAX_RENDERED = 200_000
# A loop inside a loop at most: each list is bounded, so is the work.
MAX_LOOP_DEPTH = 2
MAX_LIST_ITEMS = 100
MAX_NESTING = 4

ALLOWED_TAGS = (
    "comment",
    "cycle",
    "filter",
    "firstof",
    "for",
    "if",
    "ifchanged",
    "now",
    "resetcycle",
    "spaceless",
    "templatetag",
    "verbatim",
    "with",
)
ALLOWED_FILTERS = (
    "add",
    "capfirst",
    "cut",
    "date",
    "default",
    "default_if_none",
    "divisibleby",
    "escape",
    "first",
    "force_escape",
    "join",
    "last",
    "length",
    "linebreaks",
    "linebreaksbr",
    "lower",
    "pluralize",
    "slice",
    "striptags",
    "time",
    "timesince",
    "timeuntil",
    "title",
    "truncatechars",
    "truncatewords",
    "upper",
    "urlencode",
    "urlize",
    "wordcount",
    "yesno",
)


def _library():
    library = Library()
    for name in ALLOWED_TAGS:
        library.tags[name] = defaulttags.register.tags[name]
    for name in ALLOWED_FILTERS:
        library.filters[name] = defaultfilters.register.filters[name]
    return library


_LIBRARY = _library()


class _SandboxEngine(Engine):
    """An engine that knows the allowed tags and filters only, and no
    template but the one handed to it."""

    default_builtins = []

    def __init__(self):
        super().__init__(
            dirs=[],
            app_dirs=False,
            loaders=[],
            libraries={},
            builtins=[],
            debug=False,
            string_if_invalid="",
        )
        self.template_builtins = [_LIBRARY]


_ENGINE = _SandboxEngine()


class OverrideError(Exception):
    """An override that cannot be rendered: ``errors`` by field."""

    def __init__(self, errors):
        super().__init__(errors)
        self.errors = errors


# =============================================================================
# The mail types a call sends
# =============================================================================


@dataclasses.dataclass(frozen=True)
class MailType:
    mail_type: str
    variant: str
    label: str
    audience: str
    # (name, description, example) of each variable the wording may use.
    variables: tuple

    @property
    def key(self):
        return mail_key(self.mail_type, self.variant)


def mail_key(mail_type, variant=""):
    return f"{mail_type}:{variant}" if variant else mail_type


def _now():
    return timezone.now()


_SITE = ("site_name", "The portal's name.", lambda: config.SITE_NAME)
_PROPOSAL = ("proposal_name", "The proposal's name.", "Example proposal")
_CALL = ("call_name", "The call's name.", "Example call")
_ROUND = ("round_name", "The round's name.", "Example round")
_STEP = ("step_name", "The workflow step's name.", "Example step")
_PROPOSAL_URL = (
    "proposal_url",
    "Link to the proposal, as the recipient opens it.",
    "https://portal.example/proposals/0/",
)

_STEP_EVENT_COMMON = (
    _SITE,
    (
        "trigger",
        "The event: step_started, step_completed, step_rejected, step_expired, "
        "deadline_approaching, changes_requested, changes_submitted, "
        "changes_lapsed, contribution_reopened or contribution_submitted.",
        "step_started",
    ),
    _STEP,
    _PROPOSAL,
    _PROPOSAL_URL,
    (
        "names_calls",
        "Whether the mail may name the call and round; false where the "
        "portal hides calls from applicants.",
        True,
    ),
    ("call_name", "The call's name, when names_calls.", "Example call"),
    ("round_name", "The round's name, when names_calls.", "Example round"),
    ("deadline", "When the step is due.", _now),
    ("days_before", "Days left, for a deadline reminder.", 3),
    ("is_applicant", "Whether the mail goes to the applicant side.", False),
    ("message", "The message of a request for changes or a reopening.", ""),
    (
        "unlocked_parts",
        "What a request for changes lets the applicant edit (a list).",
        ["description"],
    ),
    ("due_at", "When requested changes are due.", _now),
)

STEP_EVENT_APPLICANT = MailType(
    "workflow_step_event",
    "applicant",
    "Workflow step event, to the applicant",
    "applicant",
    tuple(
        (name, description, example if name != "is_applicant" else True)
        for name, description, example in _STEP_EVENT_COMMON
    ),
)
STEP_EVENT_CALL_TEAM = MailType(
    "workflow_step_event",
    "call_team",
    "Workflow step event, to the call team and evaluators",
    "call_team",
    (
        *_STEP_EVENT_COMMON,
        (
            "outcome",
            "The step's outcome, as far as the recipient may read it.",
            "accepted",
        ),
        (
            "outcome_reason",
            "The reason given with the outcome, as far as the recipient may read it.",
            "Example reason.",
        ),
        (
            "counts",
            "How far the step's contributions are: counts.submitted and "
            "counts.expected.",
            {"submitted": 1, "expected": 2},
        ),
        (
            "submitted_requests",
            "The requests answered by a submission of changes: each with "
            "step_name and unlocked_parts.",
            [{"step_name": "Example step", "unlocked_parts": ["description"]}],
        ),
        (
            "project_name",
            "The project granted, in mail to the offering teams.",
            "Example project",
        ),
        (
            "project_url",
            "Link to that project.",
            "https://portal.example/projects/0/",
        ),
    ),
)

MAIL_TYPES = (
    STEP_EVENT_APPLICANT,
    STEP_EVENT_CALL_TEAM,
    MailType(
        "proposal_state_changed",
        "",
        "Proposal state changed (decision)",
        "applicant",
        (
            _SITE,
            ("new_state", "The proposal's new state.", "accepted"),
            ("previous_state", "Its state before.", "in_review"),
            _PROPOSAL,
            _PROPOSAL_URL,
            ("project_name", "The project granted, once accepted.", None),
            ("project_url", "Link to that project.", None),
            (
                "proposal_creator_name",
                "The name of the proposal's creator.",
                "Example applicant",
            ),
            ("update_date", "When the state changed.", _now),
            ("allocation_date", "When the grant starts running.", None),
            (
                "rejection_feedback",
                "The feedback given with the decision.",
                "",
            ),
            (
                "allocated_resources",
                "The resources granted: each with name, provider_name and plan_name.",
                [],
            ),
            ("call_name", "The call's name, where calls are named.", "Example call"),
            ("review_period", "The round's review period in days.", 30),
            ("duration", "The requested or granted duration.", None),
        ),
    ),
    MailType(
        "new_proposal_submitted",
        "",
        "Proposal submitted, to the call managers",
        "call_manager",
        (
            _SITE,
            _PROPOSAL_URL,
            _PROPOSAL,
            (
                "proposal_creator_name",
                "The name of the proposal's creator.",
                "Example applicant",
            ),
            _CALL,
            _ROUND,
            ("submission_date", "When it was submitted.", _now),
        ),
    ),
    MailType(
        "review_assigned",
        "",
        "Review assigned, to the reviewer",
        "reviewer",
        (
            _SITE,
            ("reviewer_name", "The reviewer's name.", "Example reviewer"),
            _CALL,
            _PROPOSAL,
            (
                "proposal_creator_name",
                "The applicant's name, where the reviewer may read it.",
                "Example applicant",
            ),
            ("submission_date", "When the proposal was submitted.", _now),
            ("review_deadline", "When the review is due.", _now),
            (
                "link_to_reviews_list",
                "Link to the reviewer's reviews.",
                "https://portal.example/reviews/",
            ),
        ),
    ),
    MailType(
        "reviews_complete",
        "",
        "All reviews in, to the call managers",
        "call_manager",
        (
            _SITE,
            _PROPOSAL,
            ("submitter_name", "The proposal's creator.", "Example applicant"),
            _CALL,
            ("reviews_count", "How many reviews were submitted.", 2),
            ("average_score", "Their average score.", 4.5),
            (
                "reviews",
                "The reviews: each with reviewer_name, score (1-5), "
                "submitted_at and criteria -- the score on each criterion of "
                "the review's step, as name, weight and score (empty when the "
                "step has no criteria).",
                [
                    {
                        "reviewer_name": "Example reviewer",
                        "score": 4,
                        "criteria": [
                            {"name": "Excellence", "weight": 2, "score": 5},
                            {"name": "Impact", "weight": 1, "score": 3},
                        ],
                        "submitted_at": _now,
                    }
                ],
            ),
            _PROPOSAL_URL,
        ),
    ),
    MailType(
        "proposal_decision_for_reviewer",
        "",
        "Decision on a reviewed proposal, to its reviewers",
        "reviewer",
        (
            _SITE,
            ("proposal_state", "The proposal's state.", "accepted"),
            _PROPOSAL_URL,
            _PROPOSAL,
            _CALL,
            ("decision_date", "When it was decided.", "January 01, 2026"),
            ("rejection_reason", "The reason of a rejection.", None),
            ("reviewer_name", "The reviewer's name.", "Example reviewer"),
        ),
    ),
    *(
        MailType(
            event,
            "",
            label,
            "proposal_role",
            (
                _SITE,
                ("holder_name", "The role holder's name.", "Example evaluator"),
                ("role_name", "The evaluator role's name.", "Rapporteur"),
                ("is_lead", "Whether they lead the role.", False),
                _PROPOSAL,
                _CALL,
                _PROPOSAL_URL,
                (
                    "coi_confirmation_required",
                    "Whether they must confirm having no conflict of interest.",
                    False,
                ),
            ),
        )
        for event, label in (
            ("proposal_role_assigned", "Evaluator role given"),
            ("proposal_role_revoked", "Evaluator role taken away"),
        )
    ),
    MailType(
        "contribution_delegated",
        "",
        "Contribution handed on",
        "delegation",
        (
            _SITE,
            ("event", "delegated, accepted, declined or revoked.", "delegated"),
            ("recipient_name", "The recipient's name.", "Example evaluator"),
            ("actor_name", "Who handed it on or answered.", "Example evaluator 2"),
            _STEP,
            ("offering_name", "The offering of the contribution.", ""),
            _PROPOSAL,
            _CALL,
            ("due_at", "When the contribution is due.", "2026-01-01"),
            _PROPOSAL_URL,
        ),
    ),
    MailType(
        "round_report_requested",
        "",
        "Round report requested, to its author",
        "round_report_author",
        (
            _SITE,
            _CALL,
            _ROUND,
            _STEP,
            ("proposals_count", "How many proposals the report covers.", 3),
            ("due_at", "When the report is due.", _now),
            ("days_left", "Days left, for a reminder.", None),
            (
                "report_url",
                "Link to the report.",
                "https://portal.example/round-reports/0/",
            ),
        ),
    ),
    MailType(
        "round_step_opened",
        "",
        "Round step opened, to the call managers",
        "call_manager",
        (
            _SITE,
            _CALL,
            _ROUND,
            _STEP,
            ("opened_by", "Who opened it.", "Example call manager"),
            ("reports_count", "How many reports were asked for.", 2),
            ("proposals_count", "How many proposals it covers.", 3),
            ("due_at", "When it is due.", _now),
            (
                "round_url",
                "Link to the call's rounds.",
                "https://portal.example/call/0/edit/?tab=rounds",
            ),
        ),
    ),
)

_BY_KEY = {(t.mail_type, t.variant): t for t in MAIL_TYPES}

# The mail types of each "always sent" mail of the workflow description.
ALWAYS_SENT_MAIL_TYPES = {
    "proposal_decided": [("proposal_state_changed", "")],
    "proposal_submitted": [("new_proposal_submitted", "")],
    "reviews_completed": [("reviews_complete", "")],
    "review_assigned": [("review_assigned", "")],
    "proposal_decided_for_reviewer": [("proposal_decision_for_reviewer", "")],
    "evaluator_role_changed": [
        ("proposal_role_assigned", ""),
        ("proposal_role_revoked", ""),
    ],
    "contribution_delegated": [("contribution_delegated", "")],
    "change_request": [
        ("workflow_step_event", "applicant"),
        ("workflow_step_event", "call_team"),
    ],
    "changes_submitted": [("workflow_step_event", "call_team")],
    "round_step_opened": [("round_step_opened", "")],
    "round_report_requested": [("round_report_requested", "")],
    "round_report_due": [("round_report_requested", "")],
}


def get_type(mail_type, variant=""):
    return _BY_KEY.get((mail_type, variant or ""))


def step_event_variant(is_applicant):
    return (
        STEP_EVENT_APPLICANT.variant if is_applicant else STEP_EVENT_CALL_TEAM.variant
    )


def type_of(mail):
    """The mail type of a built mail (``tasks.Mail``), or None for one a
    call cannot word."""
    if mail.app != APP:
        return None
    if mail.event_type == "workflow_step_event":
        return get_type(
            mail.event_type, step_event_variant(mail.audience == "applicant")
        )
    return get_type(mail.event_type, "")


def variable_names(mail_type_def):
    return [name for name, _description, _example in mail_type_def.variables]


def example_context(mail_type_def):
    context = {}
    for name, _description, example in mail_type_def.variables:
        context[name] = _example(example)
    return context


def _example(value):
    if callable(value):
        return value()
    if isinstance(value, dict):
        return {k: _example(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_example(v) for v in value]
    return value


# =============================================================================
# The sandbox
# =============================================================================


def _plain(value, depth=0):
    """``value`` as plain data: text, numbers, dates, and lists and mappings
    of them. Anything else is its text."""
    if depth > MAX_NESTING:
        return ""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        # A plain str: a value marked safe would bypass autoescaping.
        return str.__str__(value) if type(value) is not str else value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value
    if isinstance(value, dict):
        return {
            str(k): _plain(v, depth + 1)
            for k, v in list(value.items())[:MAX_LIST_ITEMS]
            if not str(k).startswith("_")
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v, depth + 1) for v in list(value)[:MAX_LIST_ITEMS]]
    return str.__str__(str(value))


def sandbox_context(context, mail_type_def):
    """The documented variables of ``mail_type_def`` from ``context``, as
    plain data: nothing else of the context reaches an override."""
    return {name: _plain(context.get(name)) for name in variable_names(mail_type_def)}


def _loop_depth(nodelist, depth=0):
    deepest = depth
    for node in nodelist:
        inner = depth + 1 if isinstance(node, ForNode) else depth
        deepest = max(deepest, inner)
        for attr in getattr(node, "child_nodelists", ()):
            child = getattr(node, attr, None)
            if child:
                deepest = max(deepest, _loop_depth(child, inner))
        # ``if`` keeps its branches in conditions_nodelists.
        for _condition, child in getattr(node, "conditions_nodelists", ()):
            deepest = max(deepest, _loop_depth(child, inner))
    return deepest


def compile_source(source, field):
    try:
        template = Template(source, engine=_ENGINE)
    except TemplateSyntaxError as error:
        raise OverrideError({field: [f"Template error: {error}"]})
    if _loop_depth(template.nodelist) > MAX_LOOP_DEPTH:
        raise OverrideError(
            {field: [f"Loops may be nested {MAX_LOOP_DEPTH} deep at most."]}
        )
    return template


def _check_sizes(subject, body_text, body_html):
    errors = {}
    for field, value, limit in (
        ("subject", subject, MAX_SUBJECT),
        ("body_text", body_text, MAX_BODY_TEXT),
        ("body_html", body_html, MAX_BODY_HTML),
    ):
        if value and len(value) > limit:
            errors[field] = [f"Ensure this field has no more than {limit} characters."]
    if errors:
        raise OverrideError(errors)


def sandbox_render(mail_type_def, subject, body_text, body_html, context):
    """``(subject, text, html)`` of an override rendered in the sandbox with
    ``context`` reduced to the documented variables. An empty HTML body is
    the text body, escaped, line breaks kept."""
    _check_sizes(subject, body_text, body_html)
    plain = sandbox_context(context, mail_type_def)
    rendered = {}
    errors = {}
    for field, source, autoescape in (
        ("subject", subject, False),
        ("body_text", body_text, False),
        ("body_html", body_html, True),
    ):
        if field == "body_html" and not (source or "").strip():
            continue
        try:
            template = compile_source(source or "", field)
            output = template.render(Context(plain, autoescape=autoescape))
        except OverrideError as error:
            errors.update(error.errors)
            continue
        except Exception as error:  # noqa: BLE001 -- told to the editor
            errors[field] = [f"The template cannot be rendered: {error}"]
            continue
        if len(output) > MAX_RENDERED:
            errors[field] = [
                f"The rendered mail is longer than {MAX_RENDERED} characters."
            ]
            continue
        rendered[field] = output
    if errors:
        raise OverrideError(errors)
    text = rendered["body_text"].strip()
    html = rendered.get("body_html")
    if html is None:
        html = defaultfilters.linebreaks_filter(text, autoescape=True)
    # A subject is one line.
    subject_line = " ".join(rendered["subject"].split())
    return subject_line, text, html


def validate(mail_type_def, subject, body_text, body_html):
    """Raise ``OverrideError`` unless the wording renders against an example
    context of its mail type."""
    errors = {}
    if not (subject or "").strip():
        errors["subject"] = ["This field may not be blank."]
    if not (body_text or "").strip():
        errors["body_text"] = ["This field may not be blank."]
    if errors:
        raise OverrideError(errors)
    sandbox_render(
        mail_type_def, subject, body_text, body_html, example_context(mail_type_def)
    )


# =============================================================================
# Resolving a mail's wording
# =============================================================================


def active_override(call_id, mail_type_def):
    if call_id is None or mail_type_def is None:
        return None
    return CallNotificationOverride.objects.filter(
        call_id=call_id,
        mail_type=mail_type_def.mail_type,
        variant=mail_type_def.variant,
        enabled=True,
    ).first()


def call_override_render(mail):
    """``(subject, text, html)`` of ``mail`` in its call's own wording, or
    None when the call has none for it, or it fails to render."""
    call = getattr(mail, "call", None)
    mail_type_def = type_of(mail)
    override = active_override(getattr(call, "pk", None), mail_type_def)
    if override is None:
        return None
    try:
        return sandbox_render(
            mail_type_def,
            override.subject,
            override.body_text,
            override.body_html,
            mail.context,
        )
    except OverrideError as error:
        logger.warning(
            "The wording of %s on call %s cannot be rendered (%s); the "
            "portal's is sent instead.",
            mail_type_def.key,
            getattr(call, "uuid", None),
            error.errors,
        )
        return None


SOURCE_CALL = "call"
SOURCE_PORTAL = "portal"
SOURCE_DEFAULT = "default"


def _paths(mail_type, template_variant=None):
    return {
        suffix: core_utils.find_template_from_registry(
            APP, mail_type, suffix, template_variant
        )
        for suffix in ("subject.txt", "message.txt", "message.html")
    }


def portal_overridden(mail_type, template_variant=None):
    """Whether staff reworded the mail for the whole portal."""
    paths = [p for p in _paths(mail_type, template_variant).values() if p]
    return (
        NotificationTemplate.objects.filter(path__in=paths).exclude(content="").exists()
    )


def render(mail):
    """``(subject, text, html, source)`` of ``mail`` as it is sent:
    ``source`` says whose wording it is."""
    rendered = call_override_render(mail)
    if rendered is not None:
        return (*rendered, SOURCE_CALL)
    subject, text, html = core_utils.render_mail(
        mail.app, mail.event_type, mail.context, mail.template_variant
    )
    source = (
        SOURCE_PORTAL
        if portal_overridden(mail.event_type, mail.template_variant)
        else SOURCE_DEFAULT
    )
    return subject, text, html, source


def portal_template_variant(mail_type_def):
    """The template set the portal words the mail with: a portal hiding
    calls from its applicants words their decision as an access request."""
    if mail_type_def.mail_type == "proposal_state_changed" and not names_calls():
        return "access_request_state_changed"
    return None


def portal_sources(mail_type_def):
    """The portal's wording of a mail type as template sources: where an
    override starts from."""
    paths = _paths(mail_type_def.mail_type, portal_template_variant(mail_type_def))
    sources = {}
    for field, suffix in (
        ("subject", "subject.txt"),
        ("body_text", "message.txt"),
        ("body_html", "message.html"),
    ):
        try:
            sources[field] = get_template(paths[suffix]).template.source.strip()
        except Exception:  # noqa: BLE001 -- a mail type without a template
            sources[field] = ""
    return sources


# =============================================================================
# Export and import
# =============================================================================

EXPORTED_FIELDS = (
    "mail_type",
    "variant",
    "subject",
    "body_text",
    "body_html",
    "enabled",
)


def dump(call):
    return [
        {field: getattr(override, field) for field in EXPORTED_FIELDS}
        for override in call.notification_overrides.order_by(
            "mail_type", "variant", "id"
        )
    ]


def describe_override(entry):
    return mail_key(entry.get("mail_type") or "", entry.get("variant") or "")
