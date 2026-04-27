"""AI Assistant tool: side-by-side comparison of publicly viewable offerings."""

import logging

from django.db.models import Q

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.marketplace.helpers import (
    is_public_marketplace_enabled,
    public_offerings_queryset,
    serialize_offering_minimal,
)
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)

# Count above which a table becomes unreadable — communicated to the LLM so
# it switches from a markdown table to bullets. Not enforced server-side.
_TABLE_READABILITY_THRESHOLD = 4


class CompareOfferingsTool(BaseTool):
    """Side-by-side comparison of 2+ publicly viewable offerings."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.COMPARE_OFFERINGS,
            category=ToolCategory.MARKETPLACE,
            description=(
                "Compare two or more publicly viewable marketplace offerings "
                "across provider, category, description and starting price.\n"
                "\n"
                "Render: 2–3 offerings → markdown comparison table "
                "(attributes as rows, offerings as columns); 4+ offerings "
                "→ bulleted sections per offering. Do NOT include a `Type` "
                "row — the offering type is a plugin identifier and is "
                "not user-facing. ALWAYS close with one inline-links line: "
                "`View [A](homeport_url) · [B](homeport_url)` using each "
                "offering's `homeport_url` field verbatim. The exact "
                "directive for this call ships in the result summary."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "uuids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Offering UUIDs from a search_offerings result "
                            "in the CURRENT turn. Use when you have them."
                        ),
                    },
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Exact offering names. Use when the user refers "
                            "to offerings surfaced in an EARLIER turn and "
                            "you only have their names in your context, not "
                            "their UUIDs."
                        ),
                    },
                },
                # At-least-2-total is enforced in execute() because the
                # count spans two fields.
            },
            usage_instructions=(
                "Use when the user wants a structured comparison of 2+ "
                "specific offerings and the data you have isn't enough. "
                "If the user only names one, use get_offering instead.\n"
                "\n"
                "Skip this tool when search_offerings already returned all "
                "the offerings this turn and its minimal fields (name, "
                "description, type, starting_price, category) cover the "
                "comparison — synthesize directly from that data.\n"
                "\n"
                "Call it when the user wants depth you don't yet have "
                "(plans, components, attributes), or the offerings came "
                "from an EARLIER turn where only names remain in context, "
                "or you need to confirm current availability.\n"
                "\n"
                "Picking `uuids` vs `names`:\n"
                "  - Just ran search_offerings this turn → pass `uuids`.\n"
                "  - Offerings from an earlier turn (only names in context) "
                "→ pass `names`. Names resolve server-side.\n"
                "  - Mixing is fine. Total across both must be ≥ 2.\n"
                "  - Never fabricate a UUID. If you don't have a real one "
                "from this turn, use `names`.\n"
                "\n"
                "RENDERING — comparison table + inline links:\n"
                "Render the comparison as a markdown table with attributes "
                "as rows and items as columns (`| Attribute | Item A | Item "
                "B |`). After the table, add a single line of inline "
                "markdown links: `View [Item A](homeport_url) · [Item "
                "B](homeport_url)`. The `homeport_url` field is on every "
                "offering — use it verbatim, never paraphrase to prose."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        if not is_public_marketplace_enabled():
            return {
                "type": "error",
                "summary": "Marketplace browsing is currently disabled.",
            }

        uuids = arguments.get("uuids") or []
        names = arguments.get("names") or []
        if len(uuids) + len(names) < 2:
            return {
                "type": "error",
                "summary": (
                    "compare_offerings needs at least 2 offerings total "
                    "across `uuids` and `names`."
                ),
            }

        filter_q = Q()
        if uuids:
            filter_q |= Q(uuid__in=uuids)
        if names:
            filter_q |= Q(name__in=names)

        offerings = list(
            public_offerings_queryset()
            .select_related("category", "customer")
            .prefetch_related("plans")
            .filter(filter_q)
            .distinct()
        )

        found_uuids = {str(o.uuid) for o in offerings}
        found_names = {o.name for o in offerings}
        missing = [u for u in uuids if u not in found_uuids] + [
            n for n in names if n not in found_names
        ]

        if not offerings:
            return {
                "type": "error",
                "summary": "None of those offerings are available.",
            }

        data = [serialize_offering_minimal(o) for o in offerings]
        use_table = len(data) < _TABLE_READABILITY_THRESHOLD
        # Repeat the rendering directive in the result summary — the LLM
        # treats tool results as higher attention than usage_instructions.
        # 2–3 items: comparison table; 4+: bullets per item (column-header
        # cramping makes a wide table unreadable).
        if use_table:
            summary = (
                f"Comparing {len(data)} offering(s). Render as a markdown "
                "comparison table (attributes as rows, offerings as "
                "columns), then close with one inline-links line: "
                "`View [A](homeport_url) · [B](homeport_url)` using each "
                "offering's `homeport_url` field verbatim."
            )
        else:
            summary = (
                f"Comparing {len(data)} offering(s). Skip the table — "
                "use one bulleted section per offering with 2–3 key "
                "points each, then close with one inline-links line: "
                "`View [A](homeport_url) · [B](homeport_url) · …` using "
                "each offering's `homeport_url` field verbatim."
            )
        if missing:
            summary += f" {len(missing)} unavailable and skipped."

        return {
            "type": "success",
            "data": {
                "offerings": data,
                "missing": missing,
                "render_as_table": use_table,
            },
            "summary": summary,
        }


tool_registry.register(CompareOfferingsTool())
