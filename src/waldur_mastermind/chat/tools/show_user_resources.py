import logging
import uuid as uuid_module

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import ResourceStates

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


class ShowUserResourcesTool(BaseTool):
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
            name=ToolName.SHOW_USER_RESOURCES,
            description="List the user's active cloud resources.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Filter resources by project UUID.",
                    },
                    "customer_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Filter resources by customer (organization) UUID.",
                    },
                    "category_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Filter resources by offering category UUID.",
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
            route_utterances=[
                "show my resources",
                "list my VMs",
                "what virtual machines do I have",
                "display my cloud instances",
                "show my deployed services",
            ],
            usage_instructions=(
                "ONLY use this tool when the user EXPLICITLY asks to "
                "see/list/display/show their actual deployed resources (VMs, instances, services):\n"
                "  CORRECT: 'show my resources', 'list my VMs', 'display my resources'\n"
                "  WRONG: 'show my offerings', 'show my projects', 'show my organizations', "
                "'show my invoices', 'show my orders'\n"
                "  WRONG: 'hello', 'what are resources?', 'how do I...', 'explain resources'\n"
                "  WRONG: 'create a VM', 'make a VM', 'provision a VM' — use the VM creation workflow instead\n"
                "\n"
                "Optional filter parameters:\n"
                "  - project_uuid: restrict to a specific project\n"
                "  - customer_uuid: restrict to a specific organization\n"
                "  - category_uuid: restrict to a specific offering category\n"
                "  - state: list of display state names to include (e.g. ['OK', 'Erred']); "
                "if omitted, terminated resources are excluded automatically\n"
                "\n"
                "If the user asks to show/list something OTHER than resources (offerings, "
                "projects, invoices, etc.), do NOT call any tool. Instead, answer with text.\n"
                "\n"
                "AFTER this tool runs, an interactive resource table widget is rendered in the UI. "
                "Your follow-up message should:\n"
                "  - NOT repeat, list, or summarize the resource data — the user already sees it\n"
                "  - Offer to help with a specific resource (e.g. view details, resize, restart, terminate)\n"
                "  - Be brief (1-2 sentences)"
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


tool_registry.register(ShowUserResourcesTool())
