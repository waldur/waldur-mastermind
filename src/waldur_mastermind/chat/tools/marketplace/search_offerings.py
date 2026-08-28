"""AI Assistant tool: keyword search over publicly viewable marketplace offerings."""

import logging

from django.db.models import Q

from waldur_mastermind.chat.tools.base import (
    MAX_LIST_RESULTS,
    BaseTool,
    ToolDefinition,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.marketplace.helpers import (
    cap_text,
    is_anonymous_caller_blocked,
    offering_homeport_url,
    offerings_queryset_for,
    serialize_offering_minimal,
)
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)


def _keyword_query(keyword: str) -> Q:
    """Build the keyword Q across every field that actually carries signal.

    Why this span: offering descriptions are frequently empty, especially for
    HPC-style entries. When the description is blank, the component catalogue
    (`GPU`, `CPU`, `RAM`, `Storage`) becomes the most reliable signal — so the
    keyword join has to cross into `components` and `plans` as well as the
    usual text fields, not just what marketplace.OfferingFilter covers.
    """
    return (
        Q(name__icontains=keyword)
        | Q(description__icontains=keyword)
        | Q(full_description__icontains=keyword)
        | Q(category__title__icontains=keyword)
        | Q(tags__name__icontains=keyword)
        | Q(components__name__icontains=keyword)
        | Q(components__type__icontains=keyword)
        | Q(plans__name__icontains=keyword)
    )


class SearchOfferingsTool(BaseTool):
    """Keyword search over publicly viewable marketplace offerings."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.SEARCH_OFFERINGS,
            category=ToolCategory.MARKETPLACE,
            description=(
                "Search publicly viewable marketplace offerings by keyword, "
                "optionally filtered by category or offering type. Returns a "
                "list of matching offerings with name, provider, country, "
                "category, starting price, detail page URL, and a "
                "`has_access_url` flag marking offerings that also publish "
                "a direct access link (get_offering reveals the URL)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": (
                            "Single strongest concrete term. Matches across "
                            "name, description, category title, tags, plan "
                            "names and component names/types. DO NOT pass a "
                            "full sentence — pick one hardware or capability "
                            "term."
                        ),
                    },
                    "category_uuid": {
                        "type": "string",
                        "description": (
                            "Optional category UUID to restrict results. "
                            "Use when you have the UUID from a prior "
                            "list_categories call in the CURRENT turn."
                        ),
                    },
                    "category_name": {
                        "type": "string",
                        "description": (
                            "Optional exact category title (e.g. 'GPU "
                            "Compute'). Use when the category was named "
                            "in an earlier turn and its UUID isn't in "
                            "your context. Pass at most one of "
                            "category_uuid / category_name."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "description": "Optional offering type (e.g. 'Marketplace.Slurm', 'OpenStack.Tenant').",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIST_RESULTS,
                        "default": MAX_LIST_RESULTS,
                        "description": (
                            "Maximum number of offerings to return. Hard "
                            f"capped at {MAX_LIST_RESULTS} regardless of "
                            "value passed."
                        ),
                    },
                },
                "required": ["keyword"],
            },
            usage_instructions=(
                "Use to discover marketplace services from a natural-"
                "language need.\n"
                "\n"
                "Query-building rules:\n"
                "1. Extract the SINGLE strongest concrete term — hardware, "
                "domain, or capability. Never a full sentence.\n"
                "   'I need GPU for climate modeling' → keyword='GPU'\n"
                "   'Looking for large storage'       → keyword='storage'\n"
                "2. Map service-shape hints to `type`:\n"
                "   'HPC / batch / supercomputer'  → 'Marketplace.Slurm'\n"
                "   'VM / cloud server / instance' → 'OpenStack.Tenant'\n"
                "   'consulting / help / training' → 'Marketplace.Basic'\n"
                "3. Empty results → try ONE broader keyword "
                "('GPU' → 'compute', 'Postgres' → 'database'), then fall "
                "back to `list_categories`.\n"
                "4. Descriptions are often blank — component names "
                "('GPU', 'CPU', 'RAM', 'Storage') usually carry more signal "
                "than the user's exact phrasing.\n"
                "5. Skip connective words ('for', 'with', 'need') and multi-"
                "word phrases — pick the hardware/category term first."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        if is_anonymous_caller_blocked(user):
            return {
                "type": "error",
                "summary": "Marketplace browsing is currently disabled.",
            }

        keyword = arguments.get("keyword", "").strip()
        category_uuid = arguments.get("category_uuid")
        category_name = arguments.get("category_name")
        offering_type = arguments.get("type")
        # The LLM may pass any limit; we hard-cap it to MAX_LIST_RESULTS so a
        # vague query that matches 50+ offerings doesn't dump them all into the
        # response.
        limit = min(int(arguments.get("limit") or MAX_LIST_RESULTS), MAX_LIST_RESULTS)

        qs = offerings_queryset_for(user)
        if keyword:
            qs = qs.filter(_keyword_query(keyword)).distinct()
        if category_uuid:
            qs = qs.filter(category__uuid=category_uuid)
        elif category_name:
            qs = qs.filter(category__title=category_name)
        if offering_type:
            qs = qs.filter(type=offering_type)

        qs = qs.select_related("category", "customer").prefetch_related("plans")
        total_count = qs.count()
        offerings = [serialize_offering_minimal(o) for o in qs[:limit]]
        truncated = total_count > limit

        if not offerings:
            summary = (
                f"No offerings match '{keyword}'. Try a broader keyword or "
                "remove category/type filters."
            )
        else:
            summary = f"Found {len(offerings)} offering(s) matching '{keyword}'."
            if truncated:
                summary += (
                    f" Showing first {limit} of {total_count} — "
                    "narrow keyword/category to see more."
                )

        return {
            "type": "success",
            "data": {
                "offerings": offerings,
                "total": len(offerings),
                "_total_count": total_count,
                "_truncated": truncated,
                "keyword": keyword,
                "category_uuid": category_uuid,
                "offering_type": offering_type,
            },
            "summary": summary,
            "ui_component": "homeport_nav",
            "ui_data": {
                "links": [
                    {
                        "label": o["name"],
                        "url": offering_homeport_url(o["uuid"]),
                        "variant": "primary",
                        # Provider/customer name shown in the link card
                        # AND persisted to the audit trail so the judge
                        # can verify the LLM's provider attributions.
                        # Without this the model often invents the
                        # provider when filtering by NCC/country.
                        "subtitle": o.get("customer_name") or "",
                        # Short description excerpt — gives the judge
                        # enough signal to verify technical-term claims
                        # (GPU types, partitions, software names) the
                        # LLM cites from real offering descriptions.
                        # Capped at 200 chars to bound audit growth
                        # (max 30 links * 200 = +6 KB per chunk, fits
                        # within TOOL_RESULTS_CHAR_CAP=12000 in
                        # chat/anonymous/judge.py).
                        # Serialized descriptions are already plain text.
                        "description_excerpt": cap_text(
                            o.get("description") or "", 200
                        ),
                    }
                    for o in offerings
                ],
                "content": summary,
            },
        }


tool_registry.register(SearchOfferingsTool())
