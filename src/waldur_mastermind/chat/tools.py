from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    """MCP-compatible tool definition."""

    name: str
    description: str
    inputSchema: dict
    meta: dict | None = Field(default=None)


TOOL_REGISTRY = {
    "show_user_resources": ToolDefinition(
        name="show_user_resources",
        description="Show all cloud resources that the current user has access to.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
}


def get_tools_prompt() -> str:
    """Generate the tools description for the system prompt."""
    tools_desc = []

    for tool in TOOL_REGISTRY.values():
        params = tool.inputSchema.get("properties", {})

        if params:
            param_lines = []
            required = tool.inputSchema.get("required", [])
            for name, info in params.items():
                param_type = info.get("type", "string")
                desc = info.get("description", "")
                is_required = name in required
                req_label = "required" if is_required else "optional"
                param_lines.append(f" - {name} ({param_type}, {req_label}) — {desc}")
            params_str = "\n".join(param_lines)
        else:
            params_str = " (no parameters)"

        tools_desc.append(
            f"- **{tool.name}**: {tool.description}\n Parameters:\n{params_str}"
        )
    return "\n\n".join(tools_desc)
