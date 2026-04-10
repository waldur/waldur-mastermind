import logging

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for chat tools.

    Handles tool registration, lookup, and auto-assembly of the tools section
    of the AI Assistant system prompt from per-tool prompt fragments.
    """

    def __init__(self):
        self._tools: dict[ToolName, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool instance."""
        if tool.name in self._tools:
            logger.warning("Tool %s already registered, overwriting", tool.name)
        self._tools[tool.name] = tool

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

        Generates behavioral guidance sections from registered tools:
        1. TOOL USAGE GUIDELINES: per-tool usage_instructions (when/when-not to use).
        2. WORKFLOWS: per-tool workflow_instructions (multi-step sequences).

        Tool schemas are passed via the API ``tools`` parameter, not injected here.
        Sections are omitted if no tools define those fragments.

        Args:
            tool_names: If provided, only include tools whose names are in this
                list. ``None`` (default) includes all tools.
        """
        if not self._tools:
            return ""

        tools = self._filter_tools(tool_names)

        sections = []

        # Section 1: Per-tool usage guidelines
        usage_parts = [
            tool.definition.usage_instructions
            for tool in tools
            if tool.definition.usage_instructions.strip()
        ]
        if usage_parts:
            sections.append(
                "=== TOOL USAGE GUIDELINES ===\n" + "\n\n".join(usage_parts)
            )

        # Section 2: Per-tool workflow instructions
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
