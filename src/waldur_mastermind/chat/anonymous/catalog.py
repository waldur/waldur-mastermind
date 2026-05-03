"""Compact catalog of public marketplace offerings injected into the
anonymous chat system prompt."""

from constance import config
from django.contrib.auth.models import AnonymousUser

from waldur_mastermind.chat.tools.marketplace.helpers import offerings_queryset_for

# Per-offering preview budgets. The catalog summary is a hint surface — the
# LLM resolves details via tool calls (get_offering / search_offerings), so
# previews only need enough signal to pick candidates. Keeping these tight
# bounds the prompt size: with ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=50, the
# worst-case catalog block is ~50 * (120 desc chars + 5 component names).
_COMPONENT_PREVIEW_LIMIT = 5
_DESCRIPTION_PREVIEW_CHARS = 120


def _format_offering_line(offering) -> str:
    name = (offering.name or "").strip().replace("|", "/")
    category = (
        offering.category.title.strip().replace("|", "/")
        if offering.category_id and offering.category
        else "—"
    )
    provider = (
        offering.customer.name.strip().replace("|", "/")
        if offering.customer_id and offering.customer
        else "—"
    )
    country = (
        (offering.customer.country or "").strip()
        if offering.customer_id and offering.customer
        else ""
    )
    provider_label = f"{provider} ({country})" if country else provider

    components = [c.type for c in offering.components.all() if c.type][
        :_COMPONENT_PREVIEW_LIMIT
    ]
    components_label = ", ".join(components) if components else "—"

    desc = (offering.description or "").strip().replace("\n", " ")
    if len(desc) > _DESCRIPTION_PREVIEW_CHARS:
        desc = desc[: _DESCRIPTION_PREVIEW_CHARS - 3] + "…"
    desc_label = f" — {desc}" if desc else ""

    return f"- {name} [{category}] | {provider_label} | components: {components_label}{desc_label}"


def build_catalog_summary() -> str:
    limit = config.ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES
    qs = (
        offerings_queryset_for(AnonymousUser())
        .select_related("category", "customer")
        .prefetch_related("components")
        .order_by("-created")
    )
    total = qs.count()
    rows = list(qs[:limit])

    if not rows:
        return "(no public offerings currently available)"

    lines = [_format_offering_line(o) for o in rows]
    if total > limit:
        lines.append(
            f"  …and {total - limit} more offering(s) not shown — narrow your "
            "request or use search_offerings for specific filters."
        )
    return "\n".join(lines)
