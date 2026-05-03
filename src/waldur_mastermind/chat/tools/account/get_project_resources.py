"""AI Assistant tool: fetch a project's resources (or search across them)."""

import logging

from waldur_mastermind.chat.tools.account.helpers import (
    name_search_filter,
    user_accessible_projects,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import (
    MAX_LIST_RESULTS,
    BaseTool,
    ToolDefinition,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import Resource

logger = logging.getLogger(__name__)


class GetProjectResourcesTool(BaseTool):
    """Return resource data (status, flavor, backend id) the LLM can narrate."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_PROJECT_RESOURCES,
            category=ToolCategory.ACCOUNT,
            description=(
                "Fetch resource data for a project or by name. Returns a "
                "list of resources with their state, offering, and backend "
                "id so the assistant can answer questions like 'what's the "
                "status of test-server-1?'. Excludes terminated resources."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Optional project UUID to restrict the listing."
                        ),
                    },
                    "project_name": {
                        "type": "string",
                        "description": (
                            "Optional project name (icontains) fallback "
                            "when no UUID is known."
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": (
                            "Optional partial resource name or backend_id "
                            "(icontains). Text only — do NOT put a UUID "
                            "here."
                        ),
                    },
                    "uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": ("Optional Resource UUID for exact match."),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for interrogative questions about resources ('what "
                "is the status of X', 'do I have Y in project Z'). "
                "For imperative 'show me my resources' use "
                "display_user_resources instead — that one renders an "
                "interactive table.\n"
                "\n"
                "Picking `uuid` vs `name`: fresh from this turn → `uuid`; "
                "from an earlier turn or typed by the user → `name`. "
                "Prefer `name` in doubt."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        project_uuid = (arguments.get("project_uuid") or "").strip()
        project_name = (arguments.get("project_name") or "").strip()
        search = (arguments.get("search") or "").strip()
        uuid = (arguments.get("uuid") or "").strip()

        if not any([project_uuid, project_name, search, uuid]):
            return {
                "type": "validation_error",
                "summary": (
                    "Pass at least one of project_uuid, project_name, search, or uuid."
                ),
            }

        for arg_name, value in [("project_uuid", project_uuid), ("uuid", uuid)]:
            if value and not validate_uuid(value):
                return {
                    "type": "validation_error",
                    "summary": f"Invalid UUID for {arg_name}: {value}",
                }

        qs = Resource.objects.all().filter_for_service_consumer(user)

        if project_uuid or project_name:
            scoped_projects = user_accessible_projects(user)
            if project_uuid:
                scoped_projects = scoped_projects.filter(uuid=project_uuid)
            elif project_name:
                scoped_projects = scoped_projects.filter(name__icontains=project_name)
            project = scoped_projects.first()
            if project is None:
                return {"type": "error", "summary": "Project not found."}
            qs = qs.filter(project=project)

        if uuid:
            qs = qs.filter(uuid=uuid)
        if search:
            qs = qs.filter(
                name_search_filter(search, extra_fields=["backend_id"])
            ).distinct()

        qs = qs.exclude(state=ResourceStates.TERMINATED).select_related(
            "offering", "offering__category", "project", "plan"
        )
        qs = qs.order_by("-created")

        total = qs.count()
        rows = list(qs[:MAX_LIST_RESULTS])
        truncated = total > MAX_LIST_RESULTS
        state_labels = dict(ResourceStates.CHOICES)

        resources = [
            {
                "uuid": str(r.uuid),
                "name": r.name,
                "state": state_labels.get(r.state, str(r.state)),
                "offering_name": r.offering.name if r.offering_id else "",
                "category_title": (
                    r.offering.category.title
                    if r.offering_id and r.offering.category_id
                    else ""
                ),
                "project_name": r.project.name if r.project_id else "",
                "project_uuid": str(r.project.uuid) if r.project_id else None,
                "backend_id": r.backend_id or "",
                "plan_name": r.plan.name if r.plan_id else "",
                "created": r.created.isoformat() if r.created else None,
            }
            for r in rows
        ]

        summary = f"Found {total} resource{'s' if total != 1 else ''}"
        if truncated:
            summary += (
                f" (showing first {MAX_LIST_RESULTS} — narrow filters to see more)"
            )
        summary += "."

        return {
            "type": "success",
            "data": {
                "resources": resources,
                "total": total,
                "_total_count": total,
                "_truncated": truncated,
            },
            "summary": summary,
        }


tool_registry.register(GetProjectResourcesTool())
