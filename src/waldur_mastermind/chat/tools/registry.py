import logging

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for chat tools.

    Handles tool registration, lookup, and auto-assembly of the tools section
    of the LLM system prompt from per-tool prompt fragments.
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool instance."""
        if tool.name in self._tools:
            logger.warning("Tool %s already registered, overwriting", tool.name)
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    @property
    def definitions(self) -> dict[str, ToolDefinition]:
        """Return {name: ToolDefinition} mapping for backward compatibility."""
        return {name: tool.definition for name, tool in self._tools.items()}

    def get_tools_prompt(self) -> str:
        """Auto-assemble the tools section of the system prompt.

        Generates three sections from registered tools:
        1. AVAILABLE TOOLS: tool names, descriptions, and parameter schemas.
        2. TOOL USAGE GUIDELINES: per-tool usage_instructions (when/when-not to use).
        3. WORKFLOWS: per-tool workflow_instructions (multi-step sequences).

        Sections 2 and 3 are omitted if no tools define those fragments.
        """
        if not self._tools:
            return ""

        sections = []

        # Section 1: Tool definitions with parameter schemas
        tool_defs = []
        for tool in self._tools.values():
            defn = tool.definition
            params = defn.inputSchema.get("properties", {})

            if params:
                param_lines = []
                required = defn.inputSchema.get("required", [])
                for param_name, info in params.items():
                    param_type = info.get("type", "string")
                    desc = info.get("description", "")
                    req_label = "required" if param_name in required else "optional"
                    param_lines.append(
                        f"  - {param_name} ({param_type}, {req_label}) — {desc}"
                    )
                params_str = "\n".join(param_lines)
            else:
                params_str = "  (no parameters)"

            tool_defs.append(
                f"- **{defn.name}**: {defn.description}\n  Parameters:\n{params_str}"
            )
        sections.append("=== AVAILABLE TOOLS ===\n" + "\n\n".join(tool_defs))

        # Section 2: Per-tool usage guidelines
        usage_parts = [
            tool.definition.usage_instructions
            for tool in self._tools.values()
            if tool.definition.usage_instructions.strip()
        ]
        if usage_parts:
            sections.append(
                "=== TOOL USAGE GUIDELINES ===\n" + "\n\n".join(usage_parts)
            )

        # Section 3: Per-tool workflow instructions
        workflow_parts = [
            tool.definition.workflow_instructions
            for tool in self._tools.values()
            if tool.definition.workflow_instructions.strip()
        ]
        if workflow_parts:
            sections.append("=== WORKFLOWS ===\n" + "\n\n".join(workflow_parts))

        return "\n\n".join(sections)


# Module-level singleton — all tools register on this instance.
tool_registry = ToolRegistry()
