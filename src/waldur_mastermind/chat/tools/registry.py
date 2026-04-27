import logging

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for chat tools.

    Handles tool registration, lookup, and auto-assembly of the tools section
    of the AI Assistant system prompt from per-tool prompt fragments.
    """

    def __init__(self):
        self._tools: dict[ToolName, BaseTool] = {}

    # Tools that opt out of the category taxonomy. They are always
    # available, never appear in the category-grouped catalog, and are
    # excluded from search_tools fetches. Keep this set tiny — every entry
    # ships unconditionally and bypasses lazy-loading.
    _META_TOOL_NAMES: set[ToolName] = {ToolName.SEARCH_TOOLS, ToolName.ASK_USER}

    def register(self, tool: BaseTool) -> None:
        """Register a tool instance.

        Every non-meta tool must declare a ``ToolCategory`` on its
        ``definition``. Only the meta-tools listed in ``_META_TOOL_NAMES``
        are allowed to have ``category=None``.
        """
        if (
            tool.definition.category is None
            and tool.definition.name not in self._META_TOOL_NAMES
        ):
            raise ValueError(
                f"Tool {tool.definition.name.value!r} must declare a "
                "ToolCategory on its ToolDefinition (only meta-tools may "
                "omit it)."
            )
        if tool.name in self._tools:
            logger.warning("Tool %s already registered, overwriting", tool.name)
        self._tools[tool.name] = tool

    def tools_by_category(self, category: ToolCategory) -> list[BaseTool]:
        """Return all registered tools whose definition.category == category.

        Preserves registration order. Tools with category=None (the meta
        tool ``search_tools``) are never returned — they opt out of the
        taxonomy.
        """
        return [
            tool
            for tool in self._tools.values()
            if tool.definition.category == category
        ]

    def get(self, name: str | ToolName) -> BaseTool | None:
        """Look up a tool by its enum member or raw string value.

        The LLM API returns plain strings for tool calls, so this method
        accepts either a :class:`ToolName` member or the underlying string
        and normalises to the enum. Unknown names return ``None``.
        """
        if not isinstance(name, ToolName):
            try:
                name = ToolName(name)
            except ValueError:
                return None
        return self._tools.get(name)

    def __contains__(self, name: str | ToolName) -> bool:
        return self.get(name) is not None

    @property
    def definitions(self) -> dict[ToolName, ToolDefinition]:
        """Return {name: ToolDefinition} mapping for backward compatibility."""
        return {name: tool.definition for name, tool in self._tools.items()}

    def _filter_tools(self, tool_names: list[ToolName] | None = None) -> list[BaseTool]:
        """Return tool instances, optionally filtered by enum membership."""
        tools = list(self._tools.values())
        if tool_names is not None:
            name_set = set(tool_names)
            tools = [t for t in tools if t.name in name_set]
        return tools

    def get_openai_tools(self, tool_names: list[ToolName] | None = None) -> list[dict]:
        """Return registered tools in OpenAI function-calling format.

        Tool schemas are sent via the API ``tools`` parameter instead of being
        injected as text into the system prompt.

        Args:
            tool_names: If provided, only include tools whose names are in this
                list. ``None`` (default) returns all tools.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.definition.name.value,
                    "description": tool.definition.description,
                    "parameters": tool.definition.inputSchema,
                },
            }
            for tool in self._filter_tools(tool_names)
        ]

    def get_tools_prompt(self, tool_names: list[ToolName] | None = None) -> str:
        """Auto-assemble the tools section of the system prompt.

        Lazy-load architecture: the prompt ships a one-line catalog of
        every permitted tool plus cross-tool workflow guidance. Full
        usage_instructions are NOT inlined — the LLM fetches them via
        ``search_tools`` when it decides which tool(s) it wants to
        invoke. Tool schemas also arrive via search_tools, not here.

        Sections emitted:
        1. TOOL CATALOG: name + one-line description, one row per tool.
        2. WORKFLOWS: cross-tool workflow_instructions (funnels, phases,
           patterns that span multiple tools). Kept inline because the
           LLM needs them to know which catalog entries to fetch.

        Args:
            tool_names: If provided, only include tools whose names are
                in this list. ``None`` (default) includes all tools.
        """
        if not self._tools:
            return ""

        tools = self._filter_tools(tool_names)

        sections: list[str] = []

        # Section 1: Catalog, grouped by ToolCategory. Uncategorised tools
        # (search_tools itself) are excluded — the LLM always has
        # search_tools available and never needs to fetch its schema.
        # Each catalog entry is the tool's own ToolDefinition.description,
        # which must stay short and parameter-free: it is also what the
        # LLM sees on the OpenAI function spec after search_tools loads
        # the schema, so one string has to serve both surfaces.
        tools_by_cat: dict[ToolCategory, list[BaseTool]] = {
            cat: [] for cat in ToolCategory
        }
        for tool in tools:
            cat = tool.definition.category
            if cat is None:
                continue
            tools_by_cat[cat].append(tool)

        catalog_sections: list[str] = []
        for cat in ToolCategory:
            bucket = tools_by_cat[cat]
            if not bucket:
                continue
            lines = [f"## {cat.value}"]
            for tool in bucket:
                lines.append(
                    f"- `{tool.definition.name.value}`: {tool.definition.description}"
                )
            catalog_sections.append("\n".join(lines))

        if catalog_sections:
            sections.append(
                "=== TOOL CALLING CONTRACT ===\n"
                "Before calling any categorised tool, call "
                "`search_tools(categories=[...])` to load the schemas for "
                "the categories whose tools you plan to use. Load every "
                "category you expect to need this turn in one call "
                "(e.g. `search_tools(categories=['marketplace','vm'])` for "
                "a discovery-then-provision flow) — repeated search_tools "
                "calls waste rounds. The catalog below lists tools grouped "
                "by category with short hints only; parameters, return "
                "shapes, and error conditions are unknown until loaded. "
                "Calling an unloaded tool is rejected; never guess argument "
                "shapes.\n"
                "\n"
                "Two meta-tools are ALWAYS available and do not appear in "
                "the catalog: `search_tools` (load categorised tool schemas) "
                "and `ask_user` (ask the user 1–4 multiple-choice or free-"
                "form questions when you lack needed detail).\n"
                "\n"
                "VM creation: any 'create a VM' / 'spin up a VM' / 'set up "
                "a server' intent → load the `vm` category and call "
                "`plan_vm`. Never use `ask_user` to ask about projects, "
                "offerings, flavors, or images yourself — `plan_vm` builds "
                "those forms with the correct server-side filters.\n"
                "\n"
                "=== TOOL CATALOG ===\n" + "\n\n".join(catalog_sections)
            )

        # Section 2: Cross-tool workflow guidance (stays inline — needed
        # so the LLM knows which catalog entries to fetch for a flow).
        workflow_parts = [
            tool.definition.workflow_instructions
            for tool in tools
            if tool.definition.workflow_instructions.strip()
        ]
        if workflow_parts:
            sections.append("=== WORKFLOWS ===\n" + "\n\n".join(workflow_parts))

        return "\n\n".join(sections)


# Module-level singleton — all tools register on this instance.
tool_registry = ToolRegistry()
