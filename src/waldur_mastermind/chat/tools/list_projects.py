import logging

from django.contrib.contenttypes.models import ContentType
from django.db import models as django_models

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OfferingStates,
)
from waldur_mastermind.marketplace.models import Offering
from waldur_openstack.models import Flavor, Image, Tenant

logger = logging.getLogger(__name__)

_MAX_PROJECTS = 50


class ListProjectsTool(BaseTool):
    """Lists projects where the user can create a VM."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_PROJECTS,
            description="List projects the user has access to and can create VMs in. Returns a list of project names with organizations. Use when the user wants to create a VM but hasn't specified a project.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            route_utterances=[
                "create a VM",
                "make a virtual machine",
                "provision a new server",
                "deploy an instance",
                "spin up a VM",
            ],
            usage_instructions=(
                "ONLY use this tool when the user asks to create a VM but has NOT specified a project:\n"
                "  ✓ CORRECT: 'create a VM' (no project given)\n"
                "  ✗ WRONG: 'what is a project?', 'explain projects', 'list my resources'"
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        accessible = filter_queryset_for_user(Project.objects.all(), user)

        if not user.is_staff:
            project_ct = ContentType.objects.get_for_model(Project)
            customer_ct = ContentType.objects.get_for_model(Customer)

            direct_project_ids = UserRole.objects.filter(
                user=user,
                is_active=True,
                content_type=project_ct,
                role__name__in=[
                    RoleEnum.PROJECT_ADMIN,
                    RoleEnum.PROJECT_MANAGER,
                    RoleEnum.PROJECT_MEMBER,
                ],
            ).values_list("object_id", flat=True)

            customer_ids = UserRole.objects.filter(
                user=user,
                is_active=True,
                content_type=customer_ct,
                role__name__in=[
                    RoleEnum.CUSTOMER_OWNER,
                    RoleEnum.CUSTOMER_MANAGER,
                ],
            ).values_list("object_id", flat=True)

            accessible = accessible.filter(
                django_models.Q(id__in=direct_project_ids)
                | django_models.Q(customer_id__in=customer_ids)
            )

        # Further restrict to projects that have an available OpenStack Instance
        # offering whose tenant also has flavors and images synced — the same
        # conditions checked by get_offering/resolve_flavor/resolve_image, so
        # that only projects where VM creation can actually succeed are shown.
        tenant_ct = ContentType.objects.get_for_model(Tenant)
        vm_offerings = (
            Offering.objects.filter(
                type=OPENSTACK_INSTANCE_OFFERING,
                state__in=(OfferingStates.ACTIVE, OfferingStates.PAUSED),
                content_type=tenant_ct,
            )
            .filter(
                django_models.Q(customer__isnull=True)
                | django_models.Q(customer_id=django_models.OuterRef("customer_id"))
                | django_models.Q(project_id=django_models.OuterRef("id"))
            )
            .filter(
                django_models.Exists(
                    Flavor.objects.filter(
                        tenants__id=django_models.OuterRef("object_id")
                    )
                )
            )
            .filter(
                django_models.Exists(
                    Image.objects.filter(
                        tenants__id=django_models.OuterRef("object_id")
                    )
                )
            )
        )
        accessible = accessible.annotate(
            _has_vm_offering=django_models.Exists(vm_offerings)
        ).filter(_has_vm_offering=True)

        queryset = (
            accessible.select_related("customer")
            .only("uuid", "name", "customer__name")
            .order_by("name")
        )

        total = queryset.count()
        projects = [
            {
                "uuid": str(p.uuid),
                "name": p.name,
                "organization": p.customer.name if p.customer else "",
            }
            for p in queryset[:_MAX_PROJECTS]
        ]

        summary = f"Found {total} project{'s' if total != 1 else ''}"
        if total > _MAX_PROJECTS:
            summary += f" (showing first {_MAX_PROJECTS})"

        return {
            "type": "success",
            "data": {
                "projects": projects,
                "total": total,
            },
            "summary": summary,
            "ui_component": "vm_order",
            "ui_data": {
                "name": "",
                "status": "project_form",
                "projects": projects,
            },
        }


tool_registry.register(ListProjectsTool())
