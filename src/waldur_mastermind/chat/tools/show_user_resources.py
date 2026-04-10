import logging

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import Resource

logger = logging.getLogger(__name__)

_DEFAULT_RESOURCE_LIMIT = 10


class ShowUserResourcesTool(BaseTool):
    """Lists the user's active cloud resources as a table."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.SHOW_USER_RESOURCES,
            description="List the user's active cloud resources in a table.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            usage_instructions=(
                "ONLY use this tool when the user EXPLICITLY asks to "
                "see/list/display/show their actual deployed resources (VMs, instances, services):\n"
                "  ✓ CORRECT: 'show my resources', 'list my VMs', 'display my resources'\n"
                "  ✗ WRONG: 'show my offerings', 'show my projects', 'show my organizations', "
                "'show my invoices', 'show my orders'\n"
                "  ✗ WRONG: 'hello', 'what are resources?', 'how do I...', 'explain resources'\n"
                "  ✗ WRONG: 'create a VM', 'make a VM', 'provision a VM' — use the VM creation workflow instead\n"
                "\n"
                "If the user asks to show/list something OTHER than resources (offerings, "
                "projects, invoices, etc.), do NOT call any tool. Instead, answer with text."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        queryset = (
            filter_queryset_for_user(
                Resource.objects.exclude(state=ResourceStates.TERMINATED),
                user,
            )
            .select_related(
                "project", "project__customer", "offering", "offering__category"
            )
            .only(
                "uuid",
                "name",
                "state",
                "created",
                "project__uuid",
                "project__name",
                "project__customer__name",
                "offering__name",
                "offering__type",
                "offering__category__title",
            )[:_DEFAULT_RESOURCE_LIMIT]
        )

        resources = []
        for r in queryset:
            resources.append(
                {
                    "uuid": str(r.uuid),
                    "name": r.name,
                    "category": r.offering.category.title
                    if r.offering.category
                    else "",
                    "offering": r.offering.name,
                    "organization": (
                        r.project.customer.name
                        if r.project and r.project.customer
                        else ""
                    ),
                    "project": r.project.name if r.project else "",
                    "project_uuid": str(r.project.uuid) if r.project else None,
                    "state": r.get_state_display(),
                }
            )

        total = len(resources)
        summary = f"Found {total} resource{'s' if total != 1 else ''}"

        headers = ["Name", "Category", "Offering", "Organization", "Project", "State"]
        rows = [
            [
                res["name"],
                res["category"],
                res["offering"],
                res["organization"],
                res["project"],
                res["state"],
            ]
            for res in resources
        ]

        logger.debug(
            "Resource query successful",
            extra={"user_id": user.id, "total_count": total},
        )

        return {
            "type": "success",
            "data": {
                "resources": resources,
                "total": total,
            },
            "summary": summary,
            "ui_component": "table",
            "ui_data": {
                "h": headers,
                "r": rows,
                "n": total,
            },
        }


tool_registry.register(ShowUserResourcesTool())
