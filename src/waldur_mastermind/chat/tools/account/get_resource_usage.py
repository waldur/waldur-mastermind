"""AI Assistant tool: per-component usage for one resource, current billing period."""

import logging
from datetime import date

from waldur_mastermind.chat.tools.account.helpers import validate_uuid
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import ComponentUsage, Resource

logger = logging.getLogger(__name__)


def _current_period_bounds(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    if start.month == 12:
        next_start = start.replace(year=start.year + 1, month=1)
    else:
        next_start = start.replace(month=start.month + 1)
    return start, next_start


class GetResourceUsageTool(BaseTool):
    """Per-component usage for a single resource in the current billing period."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_RESOURCE_USAGE,
            category=ToolCategory.ACCOUNT,
            description=(
                "Fetch component usage (CPU hours, RAM-GB-hours, storage, "
                "etc.) for one resource in the current billing period. "
                "Accepts either resource_uuid or resource_name. For the "
                "containing project's overall headroom, pair with "
                "get_project_quota."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Resource UUID (exact match).",
                    },
                    "resource_name": {
                        "type": "string",
                        "description": (
                            "Resource name (icontains) fallback when no UUID is known."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for questions like 'how much CPU have I used on X'. "
                "Prefer `resource_uuid` when you have it fresh from this "
                "turn. Pass `resource_name` when only the name is in "
                "context."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        resource_uuid = (arguments.get("resource_uuid") or "").strip()
        resource_name = (arguments.get("resource_name") or "").strip()

        if not resource_uuid and not resource_name:
            return {
                "type": "validation_error",
                "summary": "Pass resource_uuid or resource_name.",
            }
        if resource_uuid and not validate_uuid(resource_uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID for resource_uuid: {resource_uuid}",
            }

        qs = (
            Resource.objects.all()
            .filter_for_service_consumer(user)
            .select_related("offering", "project", "plan")
        )
        if resource_uuid:
            resource = qs.filter(uuid=resource_uuid).first()
        else:
            resource = qs.filter(name__icontains=resource_name).first()
        if resource is None:
            return {"type": "error", "summary": "Resource not found."}

        start, next_start = _current_period_bounds(date.today())
        usage_qs = ComponentUsage.objects.filter(
            resource=resource, billing_period=start
        ).select_related("component")
        state_labels = dict(ResourceStates.CHOICES)

        components = [
            {
                "type": u.component.type,
                "name": u.component.name,
                "measured_unit": u.component.measured_unit,
                "usage": u.usage,
            }
            for u in usage_qs
        ]

        return {
            "type": "success",
            "data": {
                "resource": {
                    "uuid": str(resource.uuid),
                    "name": resource.name,
                    "state": state_labels.get(resource.state, str(resource.state)),
                    "offering_name": (
                        resource.offering.name if resource.offering_id else ""
                    ),
                    "project_name": (
                        resource.project.name if resource.project_id else ""
                    ),
                    "plan_name": resource.plan.name if resource.plan_id else "",
                },
                "components": components,
                "period": {
                    "start": start.isoformat(),
                    "end": next_start.isoformat(),
                },
            },
            "summary": f"Usage for {resource.name} ({start.isoformat()}).",
        }


tool_registry.register(GetResourceUsageTool())
