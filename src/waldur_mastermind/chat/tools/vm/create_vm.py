import logging

from django.core.exceptions import PermissionDenied
from django.db import transaction
from rest_framework.exceptions import ValidationError

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.vm.helpers import (
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
            category=ToolCategory.VM,
            description=(
                "Create the OpenStack VM after the user confirms the plan_vm "
                "preview. Use ONLY after plan_vm returned a vm_order preview "
                "and the user said 'yes'/'proceed'/'create'. Never call "
                "without a preview first."
            ),
            usage_instructions=(
                "Call only after plan_vm has returned a vm_order preview AND "
                "the user has explicitly confirmed (preview Create button or "
                "'yes'/'proceed'/'create' message). Use the same field values "
                "the preview was rendered from. 'modify'/'change' → re-call "
                "plan_vm, not create_vm.\n"
                "\n"
                "`project_uuid` for fresh IDs from this turn's plan_vm reply; "
                "`project_name` when typed by the user or from an earlier "
                "turn. Prefer `project_name` in doubt. Never fabricate a UUID."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Project UUID. Use when you have it fresh from "
                            "a recent plan_vm "
                            "call."
                        ),
                    },
                    "project_name": {
                        "type": "string",
                        "description": (
                            "Exact project name. Use when the user named "
                            "the project and the UUID isn't in your "
                            "context. Names match exactly (case-insensitive)."
                        ),
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
                        "description": "Cloud-init user data script for VM initialization. Optional. "
                        "Stored and transmitted in plain text (Waldur database, OpenStack metadata "
                        "service, logs), so it must not contain unencrypted secrets such as passwords, "
                        "private keys or API tokens.",
                    },
                },
                # project_uuid/project_name at-least-one is enforced in execute()
                "required": ["name", "flavor", "image"],
            },
        )

    def execute(self, user, arguments: dict) -> dict:
        """Create an OpenStack VM by submitting a marketplace order.

        All database operations are wrapped in a transaction to ensure atomicity.
        If any step fails, all changes are rolled back to maintain consistency.
        """
        project_uuid = (arguments.get("project_uuid") or "").strip()
        project_name = (arguments.get("project_name") or "").strip()
        if not project_uuid and not project_name:
            return format_vm_error("Pass project_uuid or project_name.")

        with transaction.atomic():
            try:
                project = get_project(
                    user, project_uuid=project_uuid, project_name=project_name
                )
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

                network = arguments.get("network_uuid") or "default"
                ssh_key_name = arguments.get("ssh_key_name")
                system_volume_size = arguments.get("system_volume_size")

                return format_vm_success(
                    order,
                    flavor,
                    image,
                    project,
                    network=network,
                    ssh_key_name=ssh_key_name,
                    system_volume_size=system_volume_size,
                )
            except MultipleOfferingsAvailable:
                return format_vm_error(
                    "Multiple offerings are available for this project. "
                    "Call plan_vm first to pick one, then include the "
                    "selected offering_uuid in create_vm."
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
