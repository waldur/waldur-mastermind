"""AI Assistant tool: search the documentation of the deployed release."""

from waldur_mastermind.chat.docs import search
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.output_schemas import BOOLEAN, STRING, array, obj
from waldur_mastermind.chat.tools.registry import tool_registry

MAX_QUERY_LENGTH = 300

OUTPUT_SCHEMA = obj(
    {
        "query": STRING,
        "sections": array(
            obj(
                {
                    "title": STRING,
                    "breadcrumb": STRING,
                    "excerpt": STRING,
                    "url": STRING,
                    "enabled_here": BOOLEAN,
                }
            )
        ),
    }
)


class SearchDocsTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.SEARCH_DOCS,
            category=ToolCategory.DOCS,
            description=(
                "Search the portal's user and administrator documentation for "
                "how-to guides and explanations of portal features."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Keywords for the feature or task, e.g. "
                            "'personal access token' or 'add user to project'. "
                            "Leave out filler such as 'how do I'."
                        ),
                        "maxLength": MAX_QUERY_LENGTH,
                    },
                },
                "required": ["query"],
            },
            mcp_exposed=True,
            read_only=True,
            output_schema=OUTPUT_SCHEMA,
            usage_instructions=(
                "Use for questions about how to do something in this portal or "
                "how a portal feature works. Not for the user's own data (use "
                "the account and marketplace tools) and not for general IT "
                "questions unrelated to the portal.\n"
                "- Answer from the returned excerpts only, and cite each "
                "section you used as a markdown link to its `url`.\n"
                "- If no section is returned, say the documentation does not "
                "cover it. Do not fill the gap from memory with menu paths or "
                "steps.\n"
                "- A section with `enabled_here: false` describes a feature "
                "that is switched off on this portal. Say it is not enabled "
                "here and do not give its steps as instructions.\n"
                "- The documentation names the product; refer to the portal "
                "by its own name instead."
            ),
            workflow_instructions=(
                "=== DOCUMENTATION ===\n"
                "For a how-to question about this portal ('how do I create a "
                "project', 'where do I find my API token', 'what does the "
                "Team tab do'), load the `docs` category and call "
                "`search_docs` before answering. Its results describe the "
                "release and configuration of this portal, which your own "
                "knowledge does not. Questions about the user's own resources, "
                "orders or costs still go to the data tools."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        query = " ".join(str(arguments.get("query") or "").split())
        if not query:
            return {
                "type": "validation_error",
                "summary": "query is required.",
            }
        query = query[:MAX_QUERY_LENGTH]

        hits = search.search(user, query)
        data = {"query": query, "sections": [hit.as_dict() for hit in hits]}
        if not hits:
            summary = (
                f"No documentation section matches {query!r}. Tell the user "
                "the documentation does not cover it; do not answer from memory."
            )
        else:
            disabled = sum(1 for hit in hits if not hit.enabled_here)
            summary = f"{len(hits)} documentation section(s) found."
            if disabled:
                summary += (
                    f" {disabled} describe(s) a feature that is not enabled on "
                    "this portal."
                )
        return {"type": "success", "summary": summary, "data": data}


tool_registry.register(SearchDocsTool())
