"""AI Assistant tool: one project in detail, with its team."""

import logging

from waldur_core.permissions.utils import get_permissions
from waldur_core.structure.views import ProjectViewSet
from waldur_mastermind.chat.tools.account.helpers import (
    invalid_uuid_message,
    iso_or_none,
    text_arguments,
    unknown_arguments,
    user_accessible_projects,
    user_role_on_customer,
    user_role_on_project,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import (
    MAX_LIST_RESULTS,
    BaseTool,
    ToolDefinition,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)


class GetProjectTool(BaseTool):
    """Return one project's details and its team members with their roles."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_PROJECT,
            category=ToolCategory.ACCOUNT,
            description=(
                "Details of a single project: organization, dates, and team "
                "members with their roles."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Project UUID (exact match).",
                    },
                    "project_name": {
                        "type": "string",
                        "description": (
                            "Project name (icontains) when no UUID is known."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for 'what's in project X', 'who is in project X', 'when "
                "does project X end', 'what's my role there'. `project.role` "
                "and `organization.role` are the caller's own roles, null when "
                "they have none there, and `is_you` marks the caller in the "
                "team. An organization owner or manager has full access to "
                "the organization's projects without a project role or a team "
                "row: never tell them they lack access or are not listed. "
                "`team` is null and listed in `restricted_fields` when the team "
                "is hidden from the user: say it is hidden, never that it is "
                "empty or that it has members. "
                "When several "
                "projects match a name, the result lists them under "
                "`matches`; ask which one, or call again with its "
                "project_uuid. For the project's resources use "
                "get_project_resources."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        error = unknown_arguments(arguments, self.definition)
        if error:
            return {"type": "validation_error", "summary": error}
        args, error = text_arguments(arguments, "project_uuid", "project_name")
        if error:
            return {"type": "validation_error", "summary": error}
        project_uuid = args["project_uuid"]
        project_name = args["project_name"]

        if not project_uuid and not project_name:
            return {
                "type": "validation_error",
                "summary": "Pass project_uuid or project_name.",
            }
        if project_uuid and not validate_uuid(project_uuid):
            return {
                "type": "validation_error",
                "summary": invalid_uuid_message("project_uuid", project_uuid),
            }

        qs = user_accessible_projects(user).select_related("customer", "type")
        if project_uuid:
            # REST retrieves terminated projects by UUID, but lists only live ones.
            qs = qs.filter(uuid=project_uuid)
        else:
            qs = qs.filter(name__icontains=project_name, is_removed=False)

        total = qs.count()
        if total == 0:
            # Hidden and missing look the same, so neither is confirmed.
            return {
                "type": "error",
                "summary": "Project not found or not visible to you.",
            }
        if total > 1 and project_name:
            # "Lab" also matches "Lab two"; one exact name is the answer.
            exact = qs.filter(name__iexact=project_name)
            if exact.count() == 1:
                qs, total = exact, 1

        # Same keys whether one project or several matched.
        data = {
            "project": None,
            "organization": None,
            "team": None,
            "restricted_fields": [],
            "matches": [],
            "_total_count": total,
            "_truncated": total > MAX_LIST_RESULTS,
        }

        if total > 1:
            rows = qs.order_by("name")[:MAX_LIST_RESULTS]
            data["matches"] = [
                {
                    "uuid": str(p.uuid),
                    "name": p.name,
                    "organization_name": p.customer.name,
                }
                for p in rows
            ]
            return {
                "type": "success",
                "data": data,
                "summary": (f"{total} projects match '{project_name}'. Ask which one."),
            }

        project = qs.get()
        data["project"] = {
            "uuid": str(project.uuid),
            "name": project.name,
            "description": project.description or "",
            "type_name": project.type.name if project.type_id else "",
            "created": iso_or_none(project.created),
            "start_date": iso_or_none(project.start_date),
            "end_date": iso_or_none(project.end_date),
            "is_terminated": project.is_removed,
            # The caller's own role, as list_projects reports it per project.
            "role": (r.value if (r := user_role_on_project(user, project)) else None),
        }
        data["organization"] = {
            "uuid": str(project.customer.uuid),
            "name": project.customer.name,
            # As list_organizations reports it per organization.
            "role": (
                r.value
                if (r := user_role_on_customer(user, project.customer))
                else None
            ),
        }

        summary = f"Project '{project.name}' in {project.customer.name}."
        if data["project"]["role"]:
            summary += f" Your role: {data['project']['role']}."
        if data["organization"]["role"]:
            summary += f" Your organization role: {data['organization']['role']}."
        # Same gate as GET /api/projects/<uuid>/list_users/.
        if ProjectViewSet().can_view_scope_team(user, project):
            roles = get_permissions(project).order_by("user__email", "role__name")
            team_total = roles.count()
            data["team"] = {
                "members": [
                    {
                        "user_uuid": str(r.user.uuid),
                        "is_you": r.user_id == user.id,
                        "full_name": r.user.full_name,
                        "email": r.user.email,
                        "role": r.role.name,
                        "expiration_time": iso_or_none(r.expiration_time),
                    }
                    for r in roles[:MAX_LIST_RESULTS]
                ],
                "_total_count": team_total,
                "_truncated": team_total > MAX_LIST_RESULTS,
            }
            summary += f" {team_total} team role{'s' if team_total != 1 else ''}."
        else:
            data["restricted_fields"] = ["team"]
            summary += (
                " The team list is hidden from this user; it may or may not have "
                "members."
            )

        return {"type": "success", "data": data, "summary": summary}


tool_registry.register(GetProjectTool())
