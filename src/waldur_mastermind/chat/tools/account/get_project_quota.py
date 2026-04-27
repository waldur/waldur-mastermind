"""AI Assistant tool: fetch a project's quota limits and current usage."""

import logging

from django.contrib.contenttypes.models import ContentType
from django.db.models import Sum

from waldur_core.quotas.models import QuotaLimit, QuotaUsage
from waldur_mastermind.chat.tools.account.helpers import (
    user_accessible_projects,
    validate_uuid,
)
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)


class GetProjectQuotaTool(BaseTool):
    """Return current quota limits and usage for a single project."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_PROJECT_QUOTA,
            category=ToolCategory.ACCOUNT,
            description=(
                "Fetch quota limits and current usage for one project. "
                "Accepts either project_uuid or project_name. If a quota "
                "is at or near its limit, follow up with search_offerings "
                "(load the `marketplace` category) so the user can shop "
                "for more capacity."
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
                            "Project name (icontains) fallback when no UUID is known."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for questions like 'what's my quota on project X?'. "
                "Prefer `project_uuid` when you have it fresh from this "
                "turn. Pass `project_name` when only the name is in "
                "context."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        project_uuid = (arguments.get("project_uuid") or "").strip()
        project_name = (arguments.get("project_name") or "").strip()

        if not project_uuid and not project_name:
            return {
                "type": "validation_error",
                "summary": "Pass project_uuid or project_name.",
            }
        if project_uuid and not validate_uuid(project_uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID for project_uuid: {project_uuid}",
            }

        qs = user_accessible_projects(user)
        if project_uuid:
            project = qs.filter(uuid=project_uuid).first()
        else:
            project = qs.filter(name__icontains=project_name).first()
        if project is None:
            return {"type": "error", "summary": "Project not found."}

        project_ct = ContentType.objects.get_for_model(project)
        limits = {
            row["name"]: row["value"]
            for row in QuotaLimit.objects.filter(
                content_type=project_ct, object_id=project.id
            ).values("name", "value")
        }
        usage_rows = (
            QuotaUsage.objects.filter(content_type=project_ct, object_id=project.id)
            .values("name")
            .annotate(total=Sum("delta"))
        )
        usages = {row["name"]: row["total"] or 0 for row in usage_rows}

        quota_names = sorted(set(limits) | set(usages))
        quotas = [
            {
                "name": name,
                "limit": limits.get(name, -1),
                "usage": usages.get(name, 0),
            }
            for name in quota_names
        ]

        return {
            "type": "success",
            "data": {
                "project": {"uuid": str(project.uuid), "name": project.name},
                "quotas": quotas,
            },
            "summary": f"Quotas for {project.name}.",
        }


tool_registry.register(GetProjectQuotaTool())
