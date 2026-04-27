"""AI Assistant tool: list organizations the user has access to."""

import logging

from waldur_mastermind.chat.tools.account.helpers import (
    name_search_filter,
    user_accessible_customers,
    user_role_on_customer,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)

_MAX_RESULTS = 50


class ListOrganizationsTool(BaseTool):
    """List the authenticated user's organizations (customers)."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_ORGANIZATIONS,
            category=ToolCategory.ACCOUNT,
            description=(
                "List organizations the user has access to. Free-text "
                "`search` narrows by name/abbreviation; `uuid` selects "
                "one organization exactly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": (
                            "Optional partial name or abbreviation to "
                            "filter by (icontains). Text only — do NOT "
                            "put a UUID here."
                        ),
                    },
                    "uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Optional Customer UUID for exact match. "
                            "Use when you have a UUID fresh from this "
                            "turn's tool output."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use when the user wants to see THEIR OWN organizations / "
                "customers / institutions — not marketplace providers. "
                "Typical starting point for a drill-down: "
                "list_organizations → list_projects → get_project_resources.\n"
                "\n"
                "Use `search` for partial-name/abbreviation filtering. "
                "Use `uuid` when you have a valid Customer UUID from this "
                "turn. Do not put UUIDs in `search`."
            ),
            workflow_instructions="""\
=== ACCOUNT NAVIGATION CHAIN ===
Drill order for self-service questions:
list_organizations → list_projects → get_project_resources /
get_project_quota / get_resource_usage. Jump in at the level the
question targets — don't force the full chain when the user names a
project or resource directly.

Bridge out:
- get_project_quota shows exhausted or near-limit headroom → load the
  `marketplace` category and call search_offerings so the user can
  shop for additional capacity.

Sibling tools that are NOT part of the drill:
- display_user_resources is a UI trigger for the literal "show my
  resources" intent — renders an interactive table, not a chain step.
- get_user_overview is support-only: a one-shot snapshot of another
  user's state, never for self-service.\
""",
        )

    def execute(self, user, arguments: dict) -> dict:
        search = (arguments.get("search") or "").strip()
        uuid = (arguments.get("uuid") or "").strip()

        if uuid and not validate_uuid(uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID: {uuid}",
            }

        qs = user_accessible_customers(user).distinct()
        if uuid:
            qs = qs.filter(uuid=uuid)
        if search:
            qs = qs.filter(
                name_search_filter(search, extra_fields=["abbreviation"])
            ).distinct()
        qs = qs.order_by("name")
        total = qs.count()
        rows = list(qs[:_MAX_RESULTS])

        organizations = [
            {
                "uuid": str(c.uuid),
                "name": c.name,
                "abbreviation": c.abbreviation or "",
                "role": (r.value if (r := user_role_on_customer(user, c)) else None),
            }
            for c in rows
        ]

        summary = f"Found {total} organization{'s' if total != 1 else ''}"
        if total > _MAX_RESULTS:
            summary += f" (showing first {_MAX_RESULTS})"
        summary += "."

        return {
            "type": "success",
            "data": {"organizations": organizations, "total": total},
            "summary": summary,
        }


tool_registry.register(ListOrganizationsTool())
