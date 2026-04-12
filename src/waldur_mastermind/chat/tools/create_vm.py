import logging

from django.core.exceptions import PermissionDenied
from django.db import transaction
from rest_framework.exceptions import ValidationError

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.vm_helpers import (
    MultipleOfferingsAvailable,
    build_order_attributes,
    format_vm_error,
    format_vm_success,
    get_offering,
    get_plan,
    get_project,
    resolve_flavor,
    resolve_image,
    resolve_security_groups,
    resolve_ssh_key,
    resolve_subnet,
    submit_order,
    validate_flavor_image,
)

logger = logging.getLogger(__name__)


class CreateVMTool(BaseTool):
    """Creates an OpenStack VM by submitting a marketplace order."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.CREATE_VM,
            description=(
                "Create the OpenStack VM after user confirms the preview. "
                "Use ONLY after showing preview_vm and receiving user confirmation (e.g., 'yes', 'proceed', 'create'). "
                "NEVER use this without showing preview first."
            ),
            route_utterances=[
                "yes create it",
                "proceed with the VM",
                "confirm and create",
                "go ahead and deploy",
                "looks good, create the VM",
            ],
            usage_instructions=(
                "ONLY use this tool after the user has confirmed a VM preview.\n"
                "NEVER use without first showing a preview result and receiving explicit user confirmation."
            ),
            workflow_instructions="""\
=== VM CREATION WORKFLOW ===
For VM creation requests, follow this EXACT sequence:

**Phase 0: Discover Projects (if needed)**
- If the user has not specified a project, call list_projects to show available projects
- Show the returned project table and ask: "Which project would you like to create the VM in?"
- Wait for the user to pick a project before continuing
- CRITICAL: When the user selects a project, use the UUID from the table (e.g. "66f82a86c3074626a825ee72a09bee67"), NOT the project name. Multiple projects can share the same name; only the UUID uniquely identifies the project.

**Phase 1: Show Configuration Form**
- Once you have the project UUID and a VM name, call preview_vm with ONLY project_uuid, name, and offering_uuid if already known (DO NOT include flavor or image)
- If preview_vm returns status="offering_form": the project has multiple providers — show the offerings table and ask "Which provider would you like to use?", wait for the user's selection, then call preview_vm again with offering_uuid included
- CRITICAL: Use the offering UUID from the table, not the name. If only one offering exists it is selected automatically and you will receive status="form" directly.
- If preview_vm returns status="form": a form with available flavor and image options is shown to the user
- Wait for the user to submit their selections from the form

**Phase 2: Show Preview**
- After user selects flavor and image, call preview_vm again with all parameters
- The preview shows Modify/Create buttons to the user
- DO NOT call preview_vm again after showing the preview

**Phase 3: Handle User Response**
- If user says "modify"/"change": Ask what to change, gather new requirements, return to Phase 2
- If user says "proceed"/"create"/"yes"/"confirm": IMMEDIATELY call create_vm tool
- CRITICAL: Never call preview_vm a third time - go straight to create_vm

**Phase 4: Report Results**
- Explain the outcome (success/pending approval/errors with actionable guidance)""",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_uuid": {
                        "type": "string",
                        "description": "UUID or name of the project where the VM should be created. The user must have access to this project.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Name for the virtual machine. Must be between 1-150 characters.",
                    },
                    "flavor": {
                        "type": "string",
                        "description": "Flavor name or description (e.g., 'small', 'medium', 'large', '2 vCPU 4GB RAM', '8GB RAM'). Will be resolved to an available flavor from the OpenStack tenant.",
                    },
                    "image": {
                        "type": "string",
                        "description": "Operating system image name or description (e.g., 'Ubuntu', 'CentOS', 'Debian', 'Windows'). Will be resolved to an available image from the OpenStack tenant.",
                    },
                    "offering_uuid": {
                        "type": "string",
                        "description": "UUID of the offering to use. Required when multiple offerings are available for the project. Obtained from a previous offering_form response.",
                    },
                    "network_uuid": {
                        "type": "string",
                        "description": "Network UUID or 'default' to use the default network. If not provided, will use the first available network.",
                    },
                    "ssh_key_name": {
                        "type": "string",
                        "description": "Name of the user's SSH key for VM access. Optional. Must be a key owned by the user.",
                    },
                    "security_groups": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of security group names to attach to the VM. Optional.",
                    },
                    "system_volume_size": {
                        "type": "integer",
                        "description": "System volume size in gigabytes (GB). Optional. If not provided, will use the image's minimum size.",
                    },
                    "user_data": {
                        "type": "string",
                        "description": "Cloud-init user data script for VM initialization. Optional.",
                    },
                },
                "required": ["project_uuid", "name", "flavor", "image"],
            },
        )

    def execute(self, user, arguments: dict) -> dict:
        """Create an OpenStack VM by submitting a marketplace order.

        All database operations are wrapped in a transaction to ensure atomicity.
        If any step fails, all changes are rolled back to maintain consistency.
        """
        with transaction.atomic():
            try:
                project = get_project(user, arguments["project_uuid"])
                offering = get_offering(
                    user, project, offering_uuid=arguments.get("offering_uuid")
                )
                tenant = offering.scope
                flavor = resolve_flavor(tenant, arguments["flavor"])
                image = resolve_image(tenant, arguments["image"])
                validate_flavor_image(flavor, image, arguments)
                subnet = resolve_subnet(tenant, arguments.get("network_uuid"))
                ssh_key = resolve_ssh_key(user, arguments.get("ssh_key_name"))
                security_groups = resolve_security_groups(
                    tenant, arguments.get("security_groups", [])
                )
                attrs = build_order_attributes(
                    arguments, flavor, image, subnet, ssh_key, security_groups
                )
                plan = get_plan(offering)
                order = submit_order(user, project, offering, plan, attrs)
                return format_vm_success(order, flavor, image, project)
            except MultipleOfferingsAvailable:
                return format_vm_error(
                    "Multiple offerings are available for this project. "
                    "Call preview_vm first (with only project_uuid and name) to see "
                    "the available offerings, then include the selected offering_uuid."
                )
            except (ValueError, ValidationError, PermissionDenied) as e:
                if isinstance(e, ValidationError):
                    detail = e.detail
                    message = (
                        str(detail[0]) if isinstance(detail, list) else str(detail)
                    )
                else:
                    message = str(e)
                return format_vm_error(message)


tool_registry.register(CreateVMTool())
