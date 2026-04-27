"""Iterative VM-creation orchestrator.

`plan_vm` is read-only and idempotent. Each call inspects what's
resolved, returns either an `ask_user_form` for the next missing field
or a `vm_order` preview when everything is resolved. The user-confirmed
commit step is the separate `create_vm` tool — `plan_vm` never writes
to the database.

State machine:
    no project                                  → needs_project
    project, >1 valid offering                  → needs_offering
    project, 1 valid offering                   → auto-resolves
    project + offering, flavor/image missing    → needs_config (1–2 Qs)
    project + offering + flavor + image, no name → needs_name
    all resolved + valid                         → ready (vm_order preview)
"""

import logging

from django.core.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.vm.helpers import (
    MultipleOfferingsAvailable,
    format_needs_config,
    format_needs_name,
    format_needs_offering,
    format_needs_project,
    format_vm_error,
    format_vm_preview,
    get_offering,
    get_project,
    list_compatible_projects,
    resolve_flavor,
    resolve_image,
    validate_flavor_image,
)
from waldur_openstack.models import Flavor, Image

logger = logging.getLogger(__name__)


class PlanVMTool(BaseTool):
    """Iteratively gather VM-creation parameters from the user."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.PLAN_VM,
            category=ToolCategory.VM,
            description=(
                "Iteratively gather VM-creation parameters from the user. "
                "Call with whatever fields you have; returns an "
                "ask_user_form for the next missing field, or a vm_order "
                "preview when everything is resolved. Always call this "
                "before create_vm — never call create_vm directly."
            ),
            usage_instructions=(
                "Iterative VM-creation funnel. Pass every field you already "
                "have — plan_vm reports only what's still missing. Re-call "
                "with the user's reply merged in until the response is a "
                "vm_order preview, then wait for confirmation and call "
                "create_vm.\n"
                "\n"
                "Always emit a one-sentence lead-in before calling that "
                "frames the next form (e.g. 'Let's set up your VM. First, "
                "pick a project:' on first call; 'Here's the preview. Click "
                "Create when ready.' once the preview returns). The form "
                "ships only the questions; the lead-in is the only framing "
                "the user sees. Don't restate resolved fields as bullets — "
                "the form already reflects them.\n"
                "\n"
                "Never call create_vm before plan_vm returns a preview. "
                "Never re-call plan_vm twice with the same arguments.\n"
                "\n"
                "`project_uuid`/`offering_uuid` for fresh IDs from this "
                "turn's reply; `*_name` for earlier-turn or user-typed values."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Fresh project UUID from a recent plan_vm reply."
                        ),
                    },
                    "project_name": {
                        "type": "string",
                        "description": (
                            "Exact project name (case-insensitive) when "
                            "the UUID isn't fresh."
                        ),
                    },
                    "offering_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Fresh offering UUID from a recent plan_vm reply.",
                    },
                    "flavor": {
                        "type": "string",
                        "description": (
                            "Flavor name resolvable against the tenant "
                            "(e.g. 'small', 'm1.medium')."
                        ),
                    },
                    "image": {
                        "type": "string",
                        "description": (
                            "Image name resolvable against the tenant "
                            "(e.g. 'Ubuntu 22.04')."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "VM name (1–150 characters).",
                    },
                    "network_uuid": {"type": "string"},
                    "ssh_key_name": {"type": "string"},
                    "security_groups": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "system_volume_size": {"type": "integer"},
                    "user_data": {"type": "string"},
                },
                "required": [],
            },
        )

    def execute(self, user, arguments: dict) -> dict:
        project_uuid = (arguments.get("project_uuid") or "").strip()
        project_name = (arguments.get("project_name") or "").strip()

        if not project_uuid and not project_name:
            return format_needs_project(list_compatible_projects(user))

        try:
            project = get_project(
                user, project_uuid=project_uuid, project_name=project_name
            )
        except (ValueError, DRFValidationError, PermissionDenied) as e:
            return format_vm_error(str(e))

        offering_uuid = (arguments.get("offering_uuid") or "").strip()
        try:
            offering = get_offering(user, project, offering_uuid=offering_uuid or None)
        except MultipleOfferingsAvailable as e:
            offerings = [{"uuid": str(o.uuid), "name": o.name} for o in e.offerings]
            return format_needs_offering(offerings)
        except (ValueError, DRFValidationError) as e:
            return format_vm_error(str(e))

        tenant = offering.scope

        flavor_query = (arguments.get("flavor") or "").strip()
        image_query = (arguments.get("image") or "").strip()
        flavor = image = None

        if flavor_query:
            try:
                flavor = resolve_flavor(tenant, flavor_query)
            except ValueError:
                flavor = None
        if image_query:
            try:
                image = resolve_image(tenant, image_query)
            except ValueError:
                image = None

        missing: list[str] = []
        if not flavor:
            missing.append("flavor")
        if not image:
            missing.append("image")

        if missing:
            flavors = [
                {"name": f.name, "cores": f.cores, "ram": f.ram}
                for f in Flavor.objects.filter(tenants=tenant).order_by("cores", "ram")
            ]
            images = [
                {"name": i.name}
                for i in Image.objects.filter(tenants=tenant).order_by("name")
            ]
            return format_needs_config(
                flavors=flavors, images=images, missing=tuple(missing)
            )

        name = (arguments.get("name") or "").strip()
        if not name:
            return format_needs_name()

        try:
            validate_flavor_image(flavor, image, arguments)
        except ValueError as e:
            return format_vm_error(str(e))

        network = arguments.get("network_uuid") or "default"
        ssh_key_name = arguments.get("ssh_key_name")
        system_volume_size = arguments.get("system_volume_size")

        return format_vm_preview(
            name,
            project,
            flavor,
            image,
            network=network,
            ssh_key_name=ssh_key_name,
            system_volume_size=system_volume_size,
        )


tool_registry.register(PlanVMTool())
