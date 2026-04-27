import logging
import uuid as uuid_module

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import Resource

logger = logging.getLogger(__name__)

_STATE_MAP = {display: db for db, display in ResourceStates.CHOICES}


def _validate_uuid(value: str) -> bool:
    try:
        uuid_module.UUID(value)
        return True
    except ValueError:
        return False


# Input parameter names for UUID-based filter hints forwarded to the frontend.
_UUID_FILTERS = ("project_uuid", "customer_uuid", "category_uuid")
# Name-based siblings the LLM can pass when no UUID is available; the
# frontend resolves them when rendering the resource_list block.
_NAME_FILTERS = ("project_name", "customer_name", "category_name")


class DisplayUserResourcesTool(BaseTool):
    """Emits a resource_list UI signal with filter hints.

    The tool itself does not query the database — the frontend
    ``resource_list`` block fetches resources directly from the marketplace
    API (which enforces its own permissions, pagination, sorting, and
    counting). This tool only validates the LLM-supplied filter arguments
    and forwards the valid ones via ``ui_data``.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.DISPLAY_USER_RESOURCES,
            category=ToolCategory.ACCOUNT,
            description="List the user's active cloud resources.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Filter resources by project UUID.",
                    },
                    "project_name": {
                        "type": "string",
                        "description": (
                            "Filter resources by project name (fallback "
                            "when no UUID is known)."
                        ),
                    },
                    "customer_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Filter resources by customer (organization) UUID.",
                    },
                    "customer_name": {
                        "type": "string",
                        "description": (
                            "Filter resources by customer (organization) "
                            "name (fallback when no UUID is known)."
                        ),
                    },
                    "category_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Filter resources by offering category UUID.",
                    },
                    "category_name": {
                        "type": "string",
                        "description": (
                            "Filter resources by offering category name "
                            "(fallback when no UUID is known)."
                        ),
                    },
                    "state": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter resources by state display names "
                            "(e.g. ['OK', 'Erred', 'Creating']). "
                            "If omitted, terminated resources are excluded."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use only when the user explicitly asks to see/list/show "
                "their actual deployed resources (VMs, instances, services). "
                "Examples in scope: 'show my resources', 'list my VMs'. "
                "Out of scope: 'show my offerings / projects / invoices / "
                "orders' (different concepts), 'create a VM' (use VM "
                "creation workflow), or concept questions like 'what are "
                "resources?' (answer from knowledge).\n"
                "\n"
                "Picking `*_uuid` vs `*_name`: fresh from this turn → "
                "`*_uuid`; from an earlier turn or typed by the user → "
                "`*_name`. Prefer name in doubt.\n"
                "\n"
                "=== CRITICAL OUTPUT RULE ===\n"
                "After this tool runs, the UI renders an interactive "
                "resource table that the user sees BEFORE your reply. This "
                "tool returns no resource data to you — so you have NONE "
                "to narrate. Any table, list, or bullet of resource rows "
                "you write is fabricated and misleading.\n"
                "\n"
                "Your reply MUST:\n"
                "  • be ≤2 short sentences\n"
                "  • contain NO markdown tables (no `|`, no header "
                "separators)\n"
                "  • contain NO bulleted or numbered lists of resources\n"
                "  • NOT name, describe, count, or summarise individual "
                "resources\n"
                "  • offer ONE concrete next action (e.g. 'Want me to "
                "check the quota for a project, or look up usage for one "
                "of these?')\n"
                "\n"
                "Example of a CORRECT reply:\n"
                '  "Your resources are in the table above. Want me to '
                "check quota for a specific project or usage for one of "
                'these?"\n'
                "\n"
                "Example of a FORBIDDEN reply (do not imitate):\n"
                '  "Here are your active cloud resources:\\n\\n| Name | '
                'Type | State | …"'
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        for field in _UUID_FILTERS:
            value = arguments.get(field)
            if value and not _validate_uuid(value):
                return {
                    "type": "validation_error",
                    "summary": f"Invalid UUID for {field}: {value}",
                    "ui_component": "markdown",
                    "ui_data": {"c": f"Invalid UUID for {field}: {value}"},
                }

        ui_data = {}

        for param in _UUID_FILTERS:
            value = arguments.get(param)
            if value:
                ui_data[param] = value

        for param in _NAME_FILTERS:
            value = (arguments.get(param) or "").strip()
            if value:
                ui_data[param] = value

        state_filter = arguments.get("state")
        if state_filter:
            valid_states = [d for d in state_filter if d in _STATE_MAP]
            if valid_states:
                ui_data["state"] = valid_states
            else:
                logger.warning("Invalid state filter values: %s", state_filter)

        return {
            "type": "success",
            "summary": "Done. The results are displayed in the UI above.",
            "ui_component": "resource_list",
            "ui_data": ui_data,
        }

    def _count_resources(
        self, user, arguments: dict, valid_states: list[str] | None
    ) -> int:
        """Permission-scoped count matching the frontend's resource_list filters.

        Uses the same scoping as ``ConsumerResourceViewSet`` (the endpoint the
        frontend calls) so the number referenced in narration matches what the
        user actually sees in the rendered resource_list.
        """
        qs = Resource.objects.all().filter_for_service_consumer(user)

        project_uuid = arguments.get("project_uuid")
        if project_uuid:
            qs = qs.filter(project__uuid=project_uuid)

        customer_uuid = arguments.get("customer_uuid")
        if customer_uuid:
            qs = qs.filter(project__customer__uuid=customer_uuid)

        category_uuid = arguments.get("category_uuid")
        if category_uuid:
            qs = qs.filter(offering__category__uuid=category_uuid)

        if valid_states:
            state_ids = [_STATE_MAP[d] for d in valid_states]
            qs = qs.filter(state__in=state_ids)
        else:
            qs = qs.exclude(state=ResourceStates.TERMINATED)

        return qs.count()


tool_registry.register(DisplayUserResourcesTool())
