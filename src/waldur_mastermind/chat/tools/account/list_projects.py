"""AI Assistant tool: list projects the user has access to.

Distinct from ``plan_vm``'s project-listing step: this tool does not
filter to the VM creation funnel. Use this for general navigation and
for resolving a project name to a UUID.
"""

import logging

from waldur_mastermind.chat.tools.account.helpers import (
    name_search_filter,
    user_accessible_projects,
    user_role_on_project,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)

_MAX_RESULTS = 50


class ListProjectsTool(BaseTool):
    """List the user's projects, optionally scoped to one organization."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_PROJECTS,
            category=ToolCategory.ACCOUNT,
            description=(
                "List projects the user has access to (general navigation, "
                "not VM creation). Optional filters: `organization_uuid`/"
                "`organization_name` narrow to one customer; free-text "
                "`search` matches project name; `uuid` selects one project "
                "exactly. For VM creation flows, call plan_vm instead — it "
                "filters projects to only those where VM creation can succeed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "organization_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Optional organization (customer) UUID to "
                            "restrict the listing."
                        ),
                    },
                    "organization_name": {
                        "type": "string",
                        "description": (
                            "Optional organization name (icontains) "
                            "fallback when no UUID is known."
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": (
                            "Optional partial project name to filter by "
                            "(icontains). Text only — do NOT put a UUID "
                            "here."
                        ),
                    },
                    "uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": ("Optional Project UUID for exact match."),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for general project navigation or to resolve a project "
                "name into a UUID for follow-up tools "
                "(get_project_quota, get_project_resources).\n"
                "\n"
                "If the user intends to CREATE a VM, call plan_vm "
                "instead — its first step shows only projects where VM "
                "creation can actually succeed.\n"
                "\n"
                "Picking `uuid` vs `name`: fresh from this turn → `uuid`; "
                "from an earlier turn or typed by the user → `name`. "
                "Prefer `name` in doubt."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        organization_uuid = (arguments.get("organization_uuid") or "").strip()
        organization_name = (arguments.get("organization_name") or "").strip()
        search = (arguments.get("search") or "").strip()
        uuid = (arguments.get("uuid") or "").strip()

        for arg_name, value in [
            ("organization_uuid", organization_uuid),
            ("uuid", uuid),
        ]:
            if value and not validate_uuid(value):
                return {
                    "type": "validation_error",
                    "summary": f"Invalid UUID for {arg_name}: {value}",
                }

        qs = user_accessible_projects(user).select_related("customer")
        if organization_uuid:
            qs = qs.filter(customer__uuid=organization_uuid)
        elif organization_name:
            qs = qs.filter(customer__name__icontains=organization_name)
        if uuid:
            qs = qs.filter(uuid=uuid)
        if search:
            qs = qs.filter(name_search_filter(search)).distinct()
        qs = qs.order_by("name")

        total = qs.count()
        rows = list(qs[:_MAX_RESULTS])
        projects = [
            {
                "uuid": str(p.uuid),
                "name": p.name,
                "organization_name": p.customer.name if p.customer_id else "",
                "organization_uuid": str(p.customer.uuid) if p.customer_id else None,
                "role": (r.value if (r := user_role_on_project(user, p)) else None),
            }
            for p in rows
        ]

        summary = f"Found {total} project{'s' if total != 1 else ''}"
        if total > _MAX_RESULTS:
            summary += f" (showing first {_MAX_RESULTS})"
        summary += "."

        return {
            "type": "success",
            "data": {"projects": projects, "total": total},
            "summary": summary,
        }


tool_registry.register(ListProjectsTool())
