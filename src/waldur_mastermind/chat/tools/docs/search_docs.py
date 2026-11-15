"""AI Assistant tool: search the documentation of the deployed release."""

from waldur_mastermind.chat.docs import search
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.output_schemas import BOOLEAN, STRING, array, obj
from waldur_mastermind.chat.tools.registry import tool_registry

MAX_QUERY_LENGTH = search.MAX_QUERY_LENGTH

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
        "switched_off_here": array(obj({"name": STRING, "term": STRING})),
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
                            "English keywords for the feature or task, e.g. "
                            "'personal access token' or 'add user to project', "
                            "whatever language the user writes in: the "
                            "documentation is in English. Leave out filler "
                            "such as 'how do I', but when the user asks what "
                            "something is, what happens, or how two things "
                            "differ, keep those words: 'what is an offering', "
                            "'organization project difference'."
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
                "- Search in English, the language of the documentation, "
                "and answer in the language the user wrote in.\n"
                "- Answer from the returned excerpts only, and cite each "
                "section you used as a markdown link to its `url`.\n"
                "- Give steps only from a section that describes those steps. "
                "A section that merely mentions a feature (a menu listing, a "
                "glossary entry) is not instructions for it: say the "
                "documentation does not describe how to do it.\n"
                "- If no section answers the question, say the documentation "
                "does not cover it. Do not fill the gap from memory with menu "
                "paths or steps.\n"
                "- Search at most twice per question. If a reworded second "
                "search still does not answer it, stop searching and say so.\n"
                "- A section with `enabled_here: false` describes a feature "
                "that is switched off on this portal, or that this portal "
                "cannot confirm is switched on. Open the answer by saying it "
                "is not enabled here, or may not be, and do not give its steps "
                "as instructions.\n"
                "- `switched_off_here` names features of this portal that the "
                "query is about but that are switched off here. When the "
                "question is about one of them, say it is not enabled on this "
                "portal and that an administrator can turn it on; give no steps "
                "for it, and never say the portal lacks the feature.\n"
                "- The documentation names the product; refer to the portal "
                "by its own name instead."
            ),
            workflow_instructions=(
                "=== DOCUMENTATION ===\n"
                "For a how-to or what-is question about this portal ('how do "
                "I create a project', 'where do I find my API token', 'what "
                "is the difference between an organization and a project'), "
                "load the `docs` category and call `search_docs` before "
                "answering. Its results describe the release and "
                "configuration of this portal, which your own knowledge does "
                "not. Never say from memory that the portal does or does not "
                "support something; search first. Questions about the user's "
                "own resources, orders or costs still go to the data tools."
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
        switched_off = search.switched_off_here(user, query, hits)
        data = {
            "query": query,
            "sections": [hit.as_dict() for hit in hits],
            "switched_off_here": switched_off,
        }
        names = ", ".join(item["name"] for item in switched_off)
        if not hits and switched_off:
            summary = (
                f"No documentation section is available here for {query!r}: "
                f"it is about {names}, which is switched off on this portal. "
                "Tell the user it is not enabled here and that an "
                "administrator can turn it on."
            )
        elif not hits:
            summary = (
                f"No documentation section matches {query!r}. Tell the user "
                "the documentation does not cover it; do not answer from memory."
            )
        else:
            disabled = sum(1 for hit in hits if not hit.enabled_here)
            summary = f"{len(hits)} documentation section(s) found."
            if disabled:
                summary += (
                    f" {disabled} describe(s) a feature that is not enabled, or "
                    "not known to be enabled, on this portal."
                )
            if switched_off:
                summary += (
                    f" Switched off on this portal and matching the query: {names}."
                )
        return {"type": "success", "summary": summary, "data": data}


tool_registry.register(SearchDocsTool())
