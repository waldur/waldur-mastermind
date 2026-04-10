import logging

from django.core.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.vm_helpers import (
    MultipleOfferingsAvailable,
    format_vm_error,
    format_vm_form,
    format_vm_offering_form,
    format_vm_preview,
    get_offering,
    get_project,
    resolve_flavor,
    resolve_image,
    validate_flavor_image,
)

logger = logging.getLogger(__name__)


class PreviewVMTool(BaseTool):
    """Shows a VM configuration form or preview card before creation."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.PREVIEW_VM,
            description=(
                "Preview VM configuration or show configuration form. "
                "Call with ONLY project_uuid and name (no flavor/image) to show a form with available options. "
                "Call with all parameters to show a preview card. NEVER create a VM without showing preview first."
            ),
            usage_instructions=(
                "ONLY use this tool during the VM creation workflow after a project has been selected.\n"
                "Do NOT use for general questions about VMs or resources."
            ),
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
                        "description": "Flavor name or description (e.g., 'small', 'medium', 'large', '2 vCPU 4GB RAM', '8GB RAM'). Will be resolved to an available flavor from the OpenStack tenant. Optional - if omitted, a form with available options will be shown.",
                    },
                    "image": {
                        "type": "string",
                        "description": "Operating system image name or description (e.g., 'Ubuntu', 'CentOS', 'Debian', 'Windows'). Will be resolved to an available image from the OpenStack tenant. Optional - if omitted, a form with available options will be shown.",
                    },
                    "offering_uuid": {
                        "type": "string",
                        "description": "UUID of the offering to use. Required when multiple offerings are available for the project.",
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
                "required": ["project_uuid", "name"],
            },
        )

    def execute(self, user, arguments: dict) -> dict:
        try:
            project = get_project(user, arguments.get("project_uuid"))
            try:
                offering = get_offering(
                    user, project, offering_uuid=arguments.get("offering_uuid")
                )
            except MultipleOfferingsAvailable as e:
                return format_vm_offering_form(
                    arguments.get("name", ""), project, e.offerings
                )
            tenant = offering.scope

            # If flavor or image missing, return form with available options
            if not arguments.get("flavor") or not arguments.get("image"):
                return format_vm_form(
                    arguments.get("name", ""),
                    project,
                    tenant,
                )

            # Otherwise, continue with preview flow
            flavor = resolve_flavor(tenant, arguments["flavor"])
            image = resolve_image(tenant, arguments["image"])
            validate_flavor_image(flavor, image, arguments)

            return format_vm_preview(arguments.get("name", ""), project, flavor, image)
        except (ValueError, ValidationError, PermissionDenied) as e:
            if isinstance(e, ValidationError):
                detail = e.detail
                message = str(detail[0]) if isinstance(detail, list) else str(detail)
            else:
                message = str(e)
            return format_vm_error(message)


tool_registry.register(PreviewVMTool())
