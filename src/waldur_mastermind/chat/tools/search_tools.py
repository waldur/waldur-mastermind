"""Meta-tool: lazy-load tool schemas by category.

Architecture: the system prompt ships a category-grouped catalog of every
tool the user is permitted to invoke. The LLM has access to a single real
tool at the start of a turn — ``search_tools`` — which it calls with the
categories whose tools it wants. The streamer detects this call, fetches
the full schemas + usage_instructions for every tool in those categories,
and exposes those tools to the LLM in subsequent rounds.

Trade-off vs the static semantic router: one extra round on tool-using
turns, paid back in reduced system-prompt tokens on every round and zero
"router missed the relevant tool" misroutes.
"""

import logging

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import get_tool_set_for_user

logger = logging.getLogger(__name__)


def _tool_spec(tool) -> dict:
    # workflow_instructions is intentionally omitted: it's already inlined
    # once in the system prompt by ToolRegistry.get_tools_prompt, so
    # re-shipping it here would be pure duplication on every search_tools
    # round.
    return {
        "name": tool.definition.name.value,
        "description": tool.definition.description,
        "input_schema": tool.definition.inputSchema,
        "usage_instructions": tool.definition.usage_instructions,
    }


class SearchToolsTool(BaseTool):
    """Fetch full specs for every tool in one or more categories."""

    @property
    def definition(self) -> ToolDefinition:
        category_values = [c.value for c in ToolCategory]
        return ToolDefinition(
            name=ToolName.SEARCH_TOOLS,
            # category=None by design — this tool opts out of the taxonomy.
            description=(
                "Fetch full specifications for every tool in one or more "
                "categories. Pass EVERY category you expect to use this "
                "turn in a single call — batching is strongly preferred. "
                "Example: categories=['marketplace'] loads search_offerings, "
                "get_offering, list_categories, compare_offerings in one "
                "round. Example: categories=['marketplace','vm'] loads both "
                "groups at once. Repeated search_tools calls waste rounds."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "categories": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": category_values,
                        },
                        "minItems": 1,
                        "description": (
                            "Categories whose tools to load. Use the "
                            "category names shown as ``## <name>`` headers "
                            "in the tool catalog. Pass a single-element "
                            "array if you only need one group. Example: "
                            "['marketplace'] for an offering search-compare-"
                            "drill-down flow."
                        ),
                    },
                },
                "required": ["categories"],
            },
            usage_instructions="",
        )

    def execute(self, user, arguments: dict) -> dict:
        raw_categories = arguments.get("categories") or []
        seen: set[str] = set()
        resolved: list[ToolCategory] = []
        missing: list[str] = []
        for raw in raw_categories:
            if not isinstance(raw, str) or raw in seen:
                continue
            seen.add(raw)
            try:
                resolved.append(ToolCategory(raw))
            except ValueError:
                missing.append(raw)

        permitted_set = get_tool_set_for_user(user)
        permitted = (
            set(permitted_set) if permitted_set is not None else None
        )  # None means "all tools permitted"

        specs: list[dict] = []
        seen_names: set[ToolName] = set()
        empty: list[str] = []
        for cat in resolved:
            category_specs: list[dict] = []
            for tool in tool_registry.tools_by_category(cat):
                if permitted is not None and tool.definition.name not in permitted:
                    continue
                if tool.definition.name in seen_names:
                    continue
                seen_names.add(tool.definition.name)
                category_specs.append(_tool_spec(tool))
            if not category_specs:
                empty.append(cat.value)
            specs.extend(category_specs)

        if not specs:
            return {
                "type": "error",
                "summary": (
                    "None of those categories resolved to any permitted "
                    "tool for this user. Valid categories are listed as "
                    "``## <name>`` headers in the tool catalog at the top "
                    "of the system prompt."
                ),
                "data": {"missing": missing, "empty": empty},
            }

        summary_categories = ", ".join(c.value for c in resolved)
        summary = (
            f"Loaded {len(specs)} tool(s) from categories "
            f"{summary_categories}: " + ", ".join(s["name"] for s in specs)
        )
        if missing:
            summary += f". Unknown categories skipped: {', '.join(missing)}."
        if empty:
            summary += (
                f". Categories with no permitted tools for this user: "
                f"{', '.join(empty)}."
            )

        return {
            "type": "success",
            "data": {
                "fetched_tools": specs,
                "fetched_names": [s["name"] for s in specs],
                "missing": missing,
                "empty": empty,
            },
            "summary": summary,
        }


tool_registry.register(SearchToolsTool())
