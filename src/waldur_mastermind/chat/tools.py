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
        description="List the user's actual cloud resources in a table. Use ONLY when the user explicitly asks to see/show/list/display their resources. NEVER use for questions starting with 'what/how/why' - those are conceptual questions requiring explanations, not data retrieval. Examples: USE for 'show my resources', 'I want to see resources'. DO NOT USE for 'what are resources?', 'what is resource management?'.",
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


TOOL_INSTRUCTIONS = """{tools}

=== CRITICAL: TOOLS ARE EXTREMELY RARE ===
Tools should ONLY be used for showing actual data. Most requests do NOT need tools.

NEVER use tools for:
- Greetings: "hello", "hi", "hey" → Respond naturally
- Questions: "what", "why", "how", "explain" → Answer conceptually
- General conversation: "thanks", "tell me about", "help me understand" → Answer directly

ONLY use show_user_resources when EXPLICITLY asked to see/list/display/show user's actual resources:
✓ CORRECT: "show my resources", "list my VMs", "display my resources"
✗ WRONG: "hello", "what are resources?", "create code", "how do I...", "explain resources"

When using a tool:
- Respond with ONLY the JSON object
- Format: {{"tool": "show_user_resources", "arguments": {{}}}}
- No prefix text, no explanation, JUST the JSON

NEVER mention tools exist to the user.
"""
