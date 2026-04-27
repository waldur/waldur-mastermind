from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName


class ToolDefinition(BaseModel):
    """MCP-compatible tool definition with prompt instructions."""

    name: ToolName
    description: str
    inputSchema: dict
    meta: dict | None = Field(default=None)

    # Functional grouping used by search_tools. Meta-tools (search_tools
    # itself) leave this None; all other tools MUST declare a category —
    # enforced at registration time by ToolRegistry.register().
    category: ToolCategory | None = None

    # Per-tool prompt fragments, auto-assembled into the system prompt by ToolRegistry.
    # usage_instructions: when/when-not to use this tool (decision heuristics for AI Assistant).
    # workflow_instructions: multi-step sequences involving this tool (e.g. VM creation phases).
    usage_instructions: str = ""
    workflow_instructions: str = ""


class BaseTool(ABC):
    """Abstract base class for all chat tools.

    Each tool is self-contained: it owns its definition (schema + prompt fragments)
    and its execution logic. Tools register themselves on the module-level
    tool_registry singleton at import time.

    Example:
        class MyTool(BaseTool):
            @property
            def definition(self) -> ToolDefinition:
                return ToolDefinition(
                    name="my_tool",
                    description="Does something useful.",
                    inputSchema={"type": "object", "properties": {}, "required": []},
                    usage_instructions="Use my_tool when the user asks to...",
                )

            def execute(self, user, arguments: dict) -> dict:
                return {"type": "success", "summary": "Done", ...}

        tool_registry.register(MyTool())
    """

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Return the tool's definition including schema and prompt fragments."""

    @abstractmethod
    def execute(self, user, arguments: dict) -> dict:
        """Execute the tool and return a result dict.

        Args:
            user: The authenticated Django user.
            arguments: Tool arguments from the AI Assistant (validated against inputSchema).

        Returns:
            dict with keys: type ("success" or "error"), summary (str for AI Assistant context),
            and optionally: data (dict), ui_component (str), ui_data (dict).
        """

    @property
    def name(self) -> ToolName:
        return self.definition.name
