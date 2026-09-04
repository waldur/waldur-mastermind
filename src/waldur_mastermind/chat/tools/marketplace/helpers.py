"""Shared helpers for marketplace discovery tools used by the AI Assistant.

Visibility model: callers see exactly what
``marketplace.filter_by_ordering_availability_for_user`` returns for their
``User`` — i.e. the same scope the marketplace UI shows that user.

  - ``AnonymousUser`` (or ``None``)  → only ``shared=True``,
    ``state ∈ {ACTIVE, PAUSED}`` offerings (subject to
    ``ANONYMOUS_USER_CAN_VIEW_OFFERINGS``)
  - Authenticated end user            → shared offerings + offerings tied
    to their organisation groups + offerings on their connected
    projects/customers
  - Staff / support                  → all offerings
"""

import html
import re

import nh3
from constance import config
from django.contrib.auth.models import AnonymousUser

from waldur_mastermind.marketplace import models as marketplace_models

_WHITESPACE_RE = re.compile(r"\s+")

# WYSIWYG editors emit tight markup (`<p>A</p><p>B</p>`, `<li>` runs)
# with no inter-tag whitespace; without this pre-pass adjacent blocks
# collapse into run-on words after tag stripping.
_BLOCK_TAG_RE = re.compile(
    r"(?i)</?(?:p|div|li|ul|ol|tr|td|th|table|h[1-6]|br|blockquote|section|article)\b[^>]*/?>"
)

# nh3 with tags=set() keeps anchor text but discards the href — yet
# "register here → URL" is exactly what getting_started carries. Lift the
# target out as plain text before stripping. Scheme allowlist matters:
# getting_started is NOT sanitised on write (no HTMLCleanField), and this
# pre-pass runs before nh3, so it must not rescue javascript:-style hrefs
# that nh3 would have dropped.
_ANCHOR_RE = re.compile(
    r"(?is)<a\b[^>]*href=[\"'](?P<href>(?:https?://|mailto:)[^\"']+)[\"'][^>]*>"
    r"(?P<text>.*?)</a>"
)

# Caps so one offering can't dominate the tool-result token budget.
_GETTING_STARTED_CHARS = 1000
_FULL_DESCRIPTION_CHARS = 2000

# Hard bound on the raw HTML the stripper scans. Descriptions are
# unbounded provider-editable TextFields, and _ANCHOR_RE backtracks
# quadratically on "<a" runs that never close (O(occurrences × length) —
# ~11s on 150 KB of "<a "). Every consumer caps the output at
# _FULL_DESCRIPTION_CHARS or less, so 4× that of raw input is plenty
# even for markup-heavy fragments.
_STRIP_INPUT_CHARS = 4 * _FULL_DESCRIPTION_CHARS


def strip_html_to_text(text: str) -> str:
    """Collapse an HTML fragment to single-spaced plain text.

    Waldur instances ship offering descriptions and getting-started
    guides as HTML fragments (<p>, <strong>, …). Block-level tags are
    replaced with spaces so adjacent paragraphs/list items stay
    separated, nh3 (the Rust-based sanitiser already used by
    waldur_core.core.clean_html) strips the rest including script/style
    bodies, and the entities nh3 leaves escaped (&amp;, &lt;, …) are
    decoded back to plain characters.

    The output is PLAIN TEXT for LLM prompts and logs — the entity
    decoding means entity-encoded markup comes back looking live
    (``&lt;script&gt;`` becomes ``<script>``), so it must never be
    injected into an HTML context.

    Only the first ``_STRIP_INPUT_CHARS`` of input are scanned — callers
    cap the output far below that anyway.
    """
    if not text:
        return ""
    linked = _ANCHOR_RE.sub(r"\g<text> (\g<href>)", text[:_STRIP_INPUT_CHARS])
    spaced = _BLOCK_TAG_RE.sub(" ", linked)
    plain = html.unescape(nh3.clean(spaced, tags=set(), attributes={}))
    return _WHITESPACE_RE.sub(" ", plain).strip()


def cap_text(text: str, limit: int) -> str:
    """Cap plain text at ``limit`` chars, cutting on a word boundary and
    appending an ellipsis so the model knows the content is truncated.

    Falls back to a hard slice when the word-boundary cut would keep less
    than 80% of the budget (a long unbroken token — URL, ID — near the
    start must not collapse the output to its leading word)."""
    if len(text) <= limit:
        return text
    hard = text[: limit - 1].rstrip()
    cut = hard.rsplit(" ", 1)[0].rstrip()
    if len(cut) < limit * 0.8:
        cut = hard
    return cut + "…"


def offering_country(offering) -> str:
    """Offering-level country wins over the provider's registration
    country — an NCC may publish offerings hosted elsewhere. Single
    source of the precedence rule for serializers and the prompt
    catalog."""
    provider_country = offering.customer.country if offering.customer_id else ""
    return offering.country or provider_country or ""


def is_public_marketplace_enabled() -> bool:
    """True when anonymous users may view marketplace offerings.

    Used by the anonymous chat endpoint as a master switch. Authenticated
    callers are not gated by this flag — they see what their User permits.
    """
    return bool(config.ANONYMOUS_USER_CAN_VIEW_OFFERINGS)


def is_anonymous_caller_blocked(user) -> bool:
    """True when an anon caller hits a public marketplace tool while the gate is off.

    Authenticated callers always bypass — they see what their User permits, gated
    by ``offerings_queryset_for(user)``. Only the no-login flow is governed by
    ``ANONYMOUS_USER_CAN_VIEW_OFFERINGS``.
    """
    is_anon = user is None or user.is_anonymous
    return is_anon and not is_public_marketplace_enabled()


def offerings_queryset_for(user=None):
    """User-aware queryset of marketplace offerings.

    Delegates to ``filter_by_ordering_availability_for_user`` so the
    state/shared/organisation-group gates live in one place. Falls back to
    ``AnonymousUser`` when ``user`` is ``None`` — preserves the old anon-only
    behaviour for callers that haven't been updated yet.
    """
    return marketplace_models.Offering.objects.all().filter_by_ordering_availability_for_user(
        user or AnonymousUser()
    )


def get_homeport_url() -> str:
    """Homeport base URL with trailing slash stripped."""
    return (config.HOMEPORT_URL or "").rstrip("/")


def offering_homeport_url(offering_uuid) -> str:
    """Public offering detail page URL in Homeport."""
    return f"{get_homeport_url()}/marketplace-public-offering/{offering_uuid}/"


def _starting_price(offering):
    """Cheapest non-archived plan's unit_price formatted with currency.

    Returns ``"100.50 EUR"``-style strings (currency from
    ``config.CURRENCY_NAME``, default ``"EUR"``) so the LLM doesn't
    have to splice the currency code in itself — embedding it removes
    the chance it gets dropped from the rendered table.

    ``str(Decimal)`` would otherwise preserve the source exponent —
    a price stored as ``Decimal('0E-10')`` (zero with high precision)
    renders as the literal string ``'0E-10'``. The ``.2f`` format both
    pins decimals and normalises the value to plain notation.
    """
    cheapest = (
        offering.plans.filter(archived=False)
        .order_by("unit_price")
        .values_list("unit_price", flat=True)
        .first()
    )
    if cheapest is None:
        return None
    return f"{cheapest:.2f} {config.CURRENCY_NAME or 'EUR'}"


def serialize_offering_minimal(offering) -> dict:
    """LLM-friendly compact offering dict for search and comparison results.

    Does NOT expose ``offering.type`` (plugin identifier like
    ``Marketplace.Slurm`` / ``OpenStack.Tenant``) or the offering's API
    ``url`` — both leak into LLM narration as noise.

    DOES expose ``homeport_url``: the offering tools render results as a
    markdown table with a final ``Action`` column whose cell is a markdown
    link, and ``MarkdownBlock`` styles last-column links as a CTA button.
    Per-tool ``usage_instructions`` are explicit that the URL belongs
    ONLY inside ``[Open](url)``-style links and never in prose.
    """
    return {
        "name": offering.name,
        "uuid": str(offering.uuid),
        "category_title": offering.category.title if offering.category_id else "",
        "customer_name": offering.customer.name if offering.customer_id else "",
        "country": offering_country(offering) or None,
        "description": cap_text(strip_html_to_text(offering.description), 500),
        "starting_price": _starting_price(offering),
        # Whether the offering publishes a direct access link (Homeport's
        # "Access" button). It complements the Hub page — it does NOT mean
        # the offering can't be ordered through the Hub. Lets the LLM
        # mention the link in shortlists without a get_offering round-trip.
        "has_access_url": bool(offering.access_url),
        "homeport_url": offering_homeport_url(offering.uuid),
    }


def serialize_offering_detailed(offering) -> dict:
    """Detailed offering dict for get_offering tool (plans + components + attrs)."""
    base = serialize_offering_minimal(offering)
    base.update(
        {
            "full_description": cap_text(
                strip_html_to_text(offering.full_description),
                _FULL_DESCRIPTION_CHARS,
            ),
            # Access-route metadata: `access_url` is the provider-published
            # link Homeport shows as the "Access" button, alongside (not
            # instead of) the Hub page. None (not "") when unset, so the
            # LLM can say "not available" instead of narrating an empty
            # string.
            "access_url": offering.access_url or None,
            "getting_started": cap_text(
                strip_html_to_text(offering.getting_started),
                _GETTING_STARTED_CHARS,
            )
            or None,
            "attributes": offering.attributes or {},
            "tags": [t.name for t in offering.tags.all()],
            "plans": [
                {
                    "uuid": str(p.uuid),
                    "name": p.name,
                    "unit_price": str(p.unit_price),
                    "unit": p.unit,
                    "billing_mode": p.billing_mode,
                }
                for p in offering.plans.filter(archived=False)
            ],
            "components": [
                {
                    "type": c.type,
                    "name": c.name,
                    "measured_unit": c.measured_unit,
                    "billing_type": c.billing_type,
                }
                for c in offering.components.all()
            ],
        }
    )
    return base
