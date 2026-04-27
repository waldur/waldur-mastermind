"""AI Assistant tool: keyword search over publicly viewable marketplace offerings."""

import logging

from django.db.models import Q

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.marketplace.helpers import (
    is_public_marketplace_enabled,
    offering_homeport_url,
    public_offerings_queryset,
    serialize_offering_minimal,
)
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50


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
                "list of matching offerings with name, provider, category, "
                "type, starting price and detail page URL."
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
                        "maximum": _MAX_LIMIT,
                        "default": _DEFAULT_LIMIT,
                        "description": "Maximum number of offerings to return.",
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
        if not is_public_marketplace_enabled():
            return {
                "type": "error",
                "summary": "Marketplace browsing is currently disabled.",
            }

        keyword = arguments.get("keyword", "").strip()
        category_uuid = arguments.get("category_uuid")
        category_name = arguments.get("category_name")
        offering_type = arguments.get("type")
        limit = min(int(arguments.get("limit") or _DEFAULT_LIMIT), _MAX_LIMIT)

        qs = public_offerings_queryset()
        if keyword:
            qs = qs.filter(_keyword_query(keyword)).distinct()
        if category_uuid:
            qs = qs.filter(category__uuid=category_uuid)
        elif category_name:
            qs = qs.filter(category__title=category_name)
        if offering_type:
            qs = qs.filter(type=offering_type)

        qs = qs.select_related("category", "customer").prefetch_related("plans")[:limit]
        offerings = [serialize_offering_minimal(o) for o in qs]

        if not offerings:
            summary = (
                f"No offerings match '{keyword}'. Try a broader keyword or "
                "remove category/type filters."
            )
        else:
            summary = f"Found {len(offerings)} offering(s) matching '{keyword}'."

        return {
            "type": "success",
            "data": {
                "offerings": offerings,
                "total": len(offerings),
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
                    }
                    for o in offerings
                ],
                "content": summary,
            },
        }


tool_registry.register(SearchOfferingsTool())
