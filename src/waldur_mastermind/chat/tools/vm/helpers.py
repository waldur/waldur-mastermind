import logging
import re
import uuid as uuid_module

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import models as django_models
from django.urls import reverse
from rest_framework.exceptions import ValidationError

from waldur_core.core.enums import CoreStates
from waldur_core.core.models import SshPublicKey
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import Customer, Project
from waldur_core.structure.utils import (
    check_customer_blocked_or_archived,
    check_project_end_date,
)
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OfferingStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.models import Offering, Order, Plan, Resource
from waldur_mastermind.marketplace.utils import check_pending_order_exists
from waldur_openstack.models import Flavor, Image, SecurityGroup, SubNet, Tenant

logger = logging.getLogger(__name__)


def get_project(user, project_uuid: str = "", project_name: str = "") -> Project:
    """Resolve a Project the user can access by UUID or name.

    Pass at least one of ``project_uuid`` or ``project_name``. UUID is
    preferred when both are given. Raises ``ValueError`` with a friendly
    message when nothing matches or the user has no access; the caller
    catches this and shapes a ``validation_error`` response.

    Splits the legacy hybrid behaviour (one ``project_uuid`` arg that
    quietly accepted UUID-or-name) into explicit args, matching the
    marketplace tool convention.
    """
    project_uuid = (project_uuid or "").strip()
    project_name = (project_name or "").strip()

    if not project_uuid and not project_name:
        raise ValueError("Pass at least one of project_uuid or project_name.")

    accessible = filter_queryset_for_user(Project.objects.all(), user)

    if project_uuid:
        try:
            uuid_obj = uuid_module.UUID(project_uuid)
        except (ValueError, AttributeError, TypeError):
            raise ValueError(f"Invalid UUID: {project_uuid}") from None
        project = accessible.filter(uuid=uuid_obj).first()
    else:
        # name__iexact preserves the existing matching semantics — VM
        # creation demands an exact match (no fuzzy LLM-side fallback)
        # since wrong project means resources land in the wrong tenant.
        project = accessible.filter(name__iexact=project_name).first()

    if not project:
        ref = project_uuid or project_name
        raise ValueError(
            f"Project '{ref}' not found or you don't have access. "
            "Call plan_vm to see the projects you can deploy into."
        )

    # Check if user has permission to create resources in this project.
    # Staff users bypass all permission checks.
    if not user.is_staff:
        # User must have a project role or customer role that allows resource creation.
        # Check project-level roles (ADMIN, MANAGER, MEMBER)
        has_project_role = UserRole.objects.filter(
            user=user,
            is_active=True,
            scope=project,
            role__name__in=[
                RoleEnum.PROJECT_ADMIN,
                RoleEnum.PROJECT_MANAGER,
                # Note: PROJECT_MEMBER is allowed to create resources per Waldur conventions
                RoleEnum.PROJECT_MEMBER,
            ],
        ).exists()

        # Check customer-level roles (OWNER, MANAGER) which cascade down
        has_customer_role = UserRole.objects.filter(
            user=user,
            is_active=True,
            scope=project.customer,
            role__name__in=[
                RoleEnum.CUSTOMER_OWNER,
                RoleEnum.CUSTOMER_MANAGER,
            ],
        ).exists()

        if not (has_project_role or has_customer_role):
            raise PermissionDenied(
                f"You don't have permission to create resources in project '{project.name}'. "
                "Contact your project administrator to request access."
            )

    return project


class MultipleOfferingsAvailable(Exception):
    """Raised when multiple valid offerings exist and the user must choose one."""

    def __init__(self, offerings):
        self.offerings = list(offerings)


def get_offerings(project: Project):
    """Return all valid OpenStack Instance offerings for a project, ordered by specificity.

    Specificity: project-scoped (0) > customer-scoped (1) > global shared (2).

    Shared offerings must have at least one non-archived plan (mirrors
    _validate_plan_for_create in marketplace serializers); private offerings
    are accepted without plans, since auto-created per-tenant Instance
    offerings are shared=False, billable=False, and have no plan.
    """
    customer = project.customer
    has_active_plan = Plan.objects.filter(
        offering=django_models.OuterRef("pk"),
        archived=False,
    )
    return (
        Offering.objects.filter(
            type=OPENSTACK_INSTANCE_OFFERING,
            state__in=(OfferingStates.ACTIVE, OfferingStates.PAUSED),
            object_id__isnull=False,
            content_type__isnull=False,
        )
        .filter(
            django_models.Q(shared=True)
            | django_models.Q(customer=customer)
            | django_models.Q(project=project)
        )
        .filter(django_models.Q(shared=False) | django_models.Exists(has_active_plan))
        .annotate(
            _specificity=django_models.Case(
                django_models.When(project=project, then=0),
                django_models.When(customer=customer, then=1),
                default=2,
                output_field=django_models.IntegerField(),
            )
        )
        .order_by("_specificity", "name")
    )


def get_offering(user, project: Project, offering_uuid: str = None) -> Offering:
    """Resolve a single OpenStack Instance offering for the project.

    If offering_uuid is given, validates and returns that specific offering.
    If not given and exactly one offering is available, returns it automatically.
    If not given and multiple offerings are available, raises MultipleOfferingsAvailable.
    """
    customer = project.customer
    offerings = get_offerings(project)

    if offering_uuid:
        try:
            uuid_obj = uuid_module.UUID(offering_uuid)
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid offering UUID: {offering_uuid}") from None
        offering = offerings.filter(uuid=uuid_obj).first()
        if not offering:
            raise ValueError(
                "The selected offering is not available for this project. "
                "Please choose a valid offering."
            )
    else:
        all_offerings = list(offerings)
        if not all_offerings:
            # Distinguish "no offering at all" from "shared offering exists
            # but has no plan" (an SP configuration bug) so admins can diagnose.
            shared_without_plan_exists = (
                Offering.objects.filter(
                    type=OPENSTACK_INSTANCE_OFFERING,
                    state__in=(OfferingStates.ACTIVE, OfferingStates.PAUSED),
                    object_id__isnull=False,
                    content_type__isnull=False,
                    shared=True,
                )
                .exclude(plans__archived=False)
                .exists()
            )
            if shared_without_plan_exists:
                raise ValueError(
                    "A shared OpenStack Instance offering exists, "
                    "but it has no active plan. Contact your administrator."
                )
            raise ValueError(
                "No OpenStack Instance offering is available for this project. "
                "Contact your administrator to set up an OpenStack offering."
            )
        if len(all_offerings) > 1:
            # Filter out shared offerings restricted by org groups before presenting
            # the list to the user, so they only see offerings they can actually use.
            if not user.is_staff:
                all_offerings = [
                    o
                    for o in all_offerings
                    if not (
                        o.shared
                        and o.organization_groups.exists()
                        and not customer.organization_groups.filter(
                            id__in=o.organization_groups.all()
                        ).exists()
                    )
                ]
            if not all_offerings:
                raise ValueError(
                    "No OpenStack Instance offering is available for this project. "
                    "Contact your administrator to set up an OpenStack offering."
                )
            if len(all_offerings) > 1:
                raise MultipleOfferingsAvailable(all_offerings)
        offering = all_offerings[0]

    # Shared offerings may be restricted by organization groups.
    # Staff users bypass this check (matches validate_public_offering logic).
    if offering.shared and not user.is_staff:
        if offering.organization_groups.exists():
            if not customer.organization_groups.filter(
                id__in=offering.organization_groups.all()
            ).exists():
                raise ValueError(
                    "This offering is not available for ordering "
                    "due to organization group restrictions."
                )

    return offering


def resolve_flavor(tenant: Tenant, flavor_query: str) -> Flavor:
    """Resolve flavor by exact or partial name match within the tenant.

    Strips parenthetical descriptions LLM might append, e.g.:
    - "m1.small (1 vCPU, 2GB RAM)" → "m1.small"
    - "tempest2 (small flavor)" → "tempest2"
    """
    # Strip parenthetical descriptions LLM might append
    if "(" in flavor_query:
        flavor_query = flavor_query[: flavor_query.index("(")].strip()

    # Try exact match first
    flavor = Flavor.objects.filter(tenants=tenant, name__iexact=flavor_query).first()
    if not flavor:
        # Try partial match, prefer shortest name (closest match)
        flavor = (
            Flavor.objects.filter(tenants=tenant, name__icontains=flavor_query)
            .order_by(django_models.functions.Length("name"))
            .first()
        )

    if not flavor:
        available = list(
            Flavor.objects.filter(tenants=tenant).values_list("name", flat=True)
        )
        raise ValueError(
            f"Flavor '{flavor_query}' not found. "
            f"Available flavors: {', '.join(available) or 'none'}"
        )
    return flavor


def resolve_image(tenant: Tenant, image_query: str) -> Image:
    """Resolve image by exact or partial name match within the tenant.

    Strips parenthetical descriptions or extra text LLM might append, e.g.:
    - "Ubuntu 22.04 (Long Term Support)" → "Ubuntu 22.04"
    - "ubuntu22.04 LTS" → "ubuntu22.04"
    """
    # Strip parenthetical descriptions LLM might append
    if "(" in image_query:
        image_query = image_query[: image_query.index("(")].strip()

    # Strip common suffixes LLM might append
    common_suffixes = [" LTS", " stable", " latest", " server", " desktop"]
    for suffix in common_suffixes:
        if image_query.endswith(suffix):
            image_query = image_query[: -len(suffix)].strip()
            break

    # Try exact match first
    image = Image.objects.filter(tenants=tenant, name__iexact=image_query).first()
    if not image:
        # Try partial match, prefer shortest name (closest match)
        image = (
            Image.objects.filter(tenants=tenant, name__icontains=image_query)
            .order_by(django_models.functions.Length("name"))
            .first()
        )

    if not image:
        available = list(
            Image.objects.filter(tenants=tenant).values_list("name", flat=True)
        )
        raise ValueError(
            f"Image '{image_query}' not found. "
            f"Available images: {', '.join(available) or 'none'}"
        )
    return image


def validate_flavor_image(flavor: Flavor, image: Image, arguments: dict):
    """Check RAM and disk compatibility between flavor, image, and volume size.

    Mirrors the checks performed by OpenStackInstanceCreateSerializer inside
    quotas_validate.  Tenant-level quota limits (cores / RAM / storage)
    are not checked here — they surface as provisioning errors if exceeded.
    """
    if image.min_ram and flavor.ram < image.min_ram:
        raise ValueError(
            f"Flavor '{flavor.name}' has {flavor.ram} MiB RAM, but image "
            f"'{image.name}' requires at least {image.min_ram} MiB."
        )

    system_volume_size = (
        arguments["system_volume_size"] * 1024
        if arguments.get("system_volume_size")
        else max(image.min_disk, 1024)
    )
    if image.min_disk and system_volume_size < image.min_disk:
        raise ValueError(
            f"System volume size ({system_volume_size} MiB) is smaller than "
            f"image '{image.name}' minimum disk requirement ({image.min_disk} MiB)."
        )


def resolve_subnet(tenant: Tenant, network_uuid=None) -> SubNet:
    """Resolve subnet by network UUID, or pick the first available in the tenant."""
    if network_uuid and network_uuid != "default":
        try:
            uuid_obj = uuid_module.UUID(network_uuid)
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid network UUID: {network_uuid}") from None

        subnet = SubNet.objects.filter(
            tenant=tenant, network__uuid=uuid_obj, state=CoreStates.OK
        ).first()
        if not subnet:
            raise ValueError(f"Network '{network_uuid}' not found or has no subnets.")
        return subnet

    subnet = SubNet.objects.filter(tenant=tenant, state=CoreStates.OK).first()
    if not subnet:
        raise ValueError("No available networks in this tenant.")
    return subnet


def resolve_ssh_key(user, key_name=None) -> SshPublicKey | None:
    """Resolve an SSH key owned by the current user, by name."""
    if not key_name:
        return None

    key = SshPublicKey.objects.filter(user=user, name=key_name).first()
    if not key:
        raise ValueError(
            f"SSH key '{key_name}' not found. Only your own SSH keys can be used."
        )
    return key


def resolve_security_groups(tenant: Tenant, group_names: list) -> list:
    """Resolve security groups by name within the tenant."""
    if not group_names:
        return []

    # Single query for all requested groups (case-insensitive)
    lower_names = {name.lower(): name for name in group_names}
    groups = list(
        SecurityGroup.objects.filter(
            tenant=tenant,
            name__iregex=r"^(%s)$" % "|".join(re.escape(n) for n in lower_names),
            state=CoreStates.OK,
        )
    )

    found = {sg.name.lower() for sg in groups}
    missing = [orig for lower, orig in lower_names.items() if lower not in found]
    if missing:
        raise ValueError(
            "Security group(s) not found in this tenant: %s" % ", ".join(missing)
        )

    # Return in the order requested
    by_lower = {sg.name.lower(): sg for sg in groups}
    return [by_lower[name.lower()] for name in group_names]


def build_order_attributes(
    arguments, flavor: Flavor, image: Image, subnet: SubNet, ssh_key, security_groups
) -> dict:
    """Assemble the order attributes dict with API URLs for the processor chain."""
    attributes = {
        "name": arguments["name"],
        "flavor": reverse("openstack-flavor-detail", kwargs={"uuid": flavor.uuid}),
        "image": reverse("openstack-image-detail", kwargs={"uuid": image.uuid}),
        "ports": [
            {"subnet": reverse("openstack-subnet-detail", kwargs={"uuid": subnet.uuid})}
        ],
        "system_volume_size": (
            arguments["system_volume_size"] * 1024
            if arguments.get("system_volume_size")
            else max(image.min_disk, 1024)
        ),
    }

    if ssh_key:
        attributes["ssh_public_key"] = reverse(
            "sshpublickey-detail", kwargs={"uuid": ssh_key.uuid}
        )

    if security_groups:
        attributes["security_groups"] = [
            {"url": reverse("openstack-sgp-detail", kwargs={"uuid": sg.uuid})}
            for sg in security_groups
        ]

    if arguments.get("user_data"):
        user_data = arguments["user_data"]
        if len(user_data) > 65535:
            raise ValueError("user_data exceeds maximum size of 65535 characters.")
        attributes["user_data"] = user_data

    return attributes


def get_plan(offering: Offering):
    """Get the first non-archived plan for the offering, or None if none exists."""
    return Plan.objects.filter(offering=offering, archived=False).first()


def submit_order(
    user, project: Project, offering: Offering, plan: Plan, attributes: dict
) -> Order:
    """Create a marketplace Resource and Order for the VM.

    Validates project/customer state before persisting, matching the
    checks performed by the standard marketplace OrderCreateSerializer.

    NOTE: Transaction atomicity is handled by the calling tool (create_vm).
    This ensures the entire VM creation workflow is atomic.
    """
    # Guard: customer must not be blocked or archived, project must not be expired.
    # These raise rest_framework ValidationError, caught by create_vm.
    check_customer_blocked_or_archived(project.customer)
    check_project_end_date(project)

    resource = Resource(
        project=project,
        offering=offering,
        plan=plan,
        attributes=attributes,
        name=attributes.get("name", ""),
        limits={},
        options={},
    )
    resource.init_cost()
    resource.save()

    if check_pending_order_exists(resource):
        raise ValidationError("Pending order for resource already exists.")

    order = Order(
        project=project,
        resource=resource,
        offering=offering,
        plan=plan,
        type=OrderTypes.CREATE,
        created_by=user,
        attributes=attributes,
    )
    order.init_cost()
    order.save()

    logger.info(
        "VM creation order submitted",
        extra={
            "user_id": user.id,
            "order_uuid": str(order.uuid),
            "project_uuid": str(project.uuid),
            "offering_uuid": str(offering.uuid),
        },
    )
    return order


def format_vm_form(name: str, project: Project, tenant: Tenant) -> dict:
    """Format a VM configuration form with available options."""
    flavors = [
        {
            "name": f.name,
            "cores": f.cores,
            "ram": f.ram,
        }
        for f in Flavor.objects.filter(tenants=tenant).order_by("cores", "ram")
    ]

    images = [
        {
            "name": i.name,
            "min_disk": i.min_disk,
            "min_ram": i.min_ram,
        }
        for i in Image.objects.filter(tenants=tenant).order_by("name")
    ]

    return {
        "type": "success",
        "summary": "VM configuration form ready",
        "ui_component": "vm_order",
        "ui_data": {
            "name": name,
            "status": "form",
            "project": project.name,
            "organization": project.customer.name,
            "project_uuid": str(project.uuid),
            "flavors": flavors,
            "images": images,
        },
    }


def format_vm_offering_form(name: str, project: Project, offerings) -> dict:
    """Format an offering selection form when multiple offerings are available."""
    return {
        "type": "success",
        "summary": "Multiple offerings available — please select one",
        "ui_component": "vm_order",
        "ui_data": {
            "name": name,
            "status": "offering_form",
            "project": project.name,
            "organization": project.customer.name,
            "project_uuid": str(project.uuid),
            "offerings": [{"uuid": str(o.uuid), "name": o.name} for o in offerings],
        },
    }


def format_vm_preview(
    name: str,
    project: Project,
    flavor: Flavor,
    image: Image,
    network: str = None,
    ssh_key_name: str = None,
    system_volume_size: int = None,
) -> dict:
    """Format a VM preview response for user confirmation."""
    ram_gb = flavor.ram / 1024  # Convert MiB to GB
    flavor_display = f"{flavor.name} ({flavor.cores} vCPU, {ram_gb:.0f}GB RAM)"

    ui_data = {
        "name": name,
        "flavor": flavor_display,
        "image": image.name,
        "status": "preview",
        "project": project.name,
        "organization": project.customer.name,
        "project_uuid": str(project.uuid),
    }
    if network:
        ui_data["network"] = network
    if ssh_key_name:
        ui_data["ssh_key_name"] = ssh_key_name
    if system_volume_size:
        ui_data["system_volume_size"] = system_volume_size

    return {
        "type": "success",
        "summary": "VM preview ready for confirmation",
        "ui_component": "vm_order",
        "ui_data": ui_data,
    }


def format_vm_success(
    order: Order,
    flavor: Flavor,
    image: Image,
    project: Project,
    network: str = None,
    ssh_key_name: str = None,
    system_volume_size: int = None,
) -> dict:
    """Format a successful VM creation order response."""
    ram_gb = flavor.ram / 1024  # Convert MiB to GB
    flavor_display = f"{flavor.name} ({flavor.cores} vCPU, {ram_gb:.0f}GB RAM)"

    ui_data = {
        "order_id": str(order.uuid),
        "name": order.attributes.get("name", ""),
        "flavor": flavor_display,
        "image": image.name,
        "status": "success",
        "message": f"VM '{order.attributes.get('name', '')}' order created successfully. Your VM will be provisioned once approved.",
        "project": project.name,
        "organization": project.customer.name,
        "project_uuid": str(project.uuid),
    }
    if network:
        ui_data["network"] = network
    if ssh_key_name:
        ui_data["ssh_key_name"] = ssh_key_name
    if system_volume_size:
        ui_data["system_volume_size"] = system_volume_size

    return {
        "type": "success",
        "summary": "VM creation order submitted successfully",
        "ui_component": "vm_order",
        "ui_data": ui_data,
    }


def format_vm_error(message: str) -> dict:
    """Format a VM creation validation error response.

    Uses type='validation_error' to distinguish user-facing validation
    errors (which should be displayed) from internal system errors
    (which should be hidden and only logged).
    """
    return {
        "type": "validation_error",
        "error": message,
        "summary": f"Failed to create VM: {message}",
        "ui_component": "vm_order",
        "ui_data": {
            "order_id": "",
            "name": "",
            "status": "error",
            "error": message,
        },
    }


_MAX_PLAN_VM_OPTIONS = 20


def list_compatible_projects(user, limit: int = 50) -> list[dict]:
    """Return projects where the user can create a VM as plain data.

    Filtering matches the criteria the deleted
    list_vm_creation_compatible_projects tool used: the user has a
    project- or customer-level role that allows resource creation, and
    the project has at least one ACTIVE/PAUSED OpenStack Instance
    offering whose tenant has flavors and images synced.
    """
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

    tenant_ct = ContentType.objects.get_for_model(Tenant)
    vm_offerings = (
        Offering.objects.filter(
            type=OPENSTACK_INSTANCE_OFFERING,
            state__in=(OfferingStates.ACTIVE, OfferingStates.PAUSED),
            content_type=tenant_ct,
        )
        .filter(
            django_models.Q(shared=True)
            | django_models.Q(customer_id=django_models.OuterRef("customer_id"))
            | django_models.Q(project_id=django_models.OuterRef("id"))
        )
        .filter(
            django_models.Exists(
                Flavor.objects.filter(tenants__id=django_models.OuterRef("object_id"))
            )
        )
        .filter(
            django_models.Exists(
                Image.objects.filter(tenants__id=django_models.OuterRef("object_id"))
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

    return [
        {
            "uuid": str(p.uuid),
            "name": p.name,
            "organization": p.customer.name if p.customer else "",
        }
        for p in queryset[:limit]
    ]


def format_needs_project(projects: list[dict]) -> dict:
    """ask_user_form asking the user which project to deploy in."""
    if not projects:
        message = "No projects with a usable VM offering. Contact your administrator."
        return {
            "type": "validation_error",
            "summary": message,
            "ui_component": "markdown",
            "ui_data": {"c": message},
        }

    options = [
        {
            "id": f"q0o{i}",
            "label": p["name"],
            "description": p["organization"],
            "value": p["uuid"],
        }
        for i, p in enumerate(projects[:_MAX_PLAN_VM_OPTIONS])
    ]
    return {
        "type": "success",
        "summary": (
            "Project picker form rendered. STOP — wait for the user's "
            "reply. Do NOT call another tool (no ask_user, no plan_vm) "
            "until they respond."
        ),
        "ui_component": "ask_user_form",
        "ui_data": {
            "questions": [
                {
                    "id": "q0",
                    "header": "Project",
                    "question": "Which project should the VM be created in?",
                    "options": options,
                    "multiSelect": False,
                    "allowFreeText": False,
                }
            ],
        },
    }


def format_needs_offering(offerings: list[dict]) -> dict:
    """ask_user_form asking which offering (provider) to use."""
    options = [
        {"id": f"q0o{i}", "label": o["name"], "value": o["uuid"]}
        for i, o in enumerate(offerings[:_MAX_PLAN_VM_OPTIONS])
    ]
    return {
        "type": "success",
        "summary": (
            "Offering picker form rendered. STOP — wait for the user's "
            "reply. Do NOT call another tool until they respond."
        ),
        "ui_component": "ask_user_form",
        "ui_data": {
            "questions": [
                {
                    "id": "q0",
                    "header": "Offering",
                    "question": "Which offering (provider) should be used?",
                    "options": options,
                    "multiSelect": False,
                    "allowFreeText": False,
                }
            ],
        },
    }


def format_needs_config(
    flavors: list[dict], images: list[dict], missing: tuple[str, ...]
) -> dict:
    """ask_user_form for whichever of flavor/image is missing.

    `missing` is a subset of {"flavor", "image"}. If both are missing we
    ask both as a 2-question form; otherwise one question.
    """
    questions: list[dict] = []
    if "flavor" in missing:
        q_idx = len(questions)
        flavor_options = [
            {
                "id": f"q{q_idx}o{i}",
                "label": f["name"],
                "description": (
                    f"{f.get('cores', '?')} vCPU, {(f.get('ram', 0) / 1024):.0f}GB RAM"
                ),
                "value": f["name"],
            }
            for i, f in enumerate(flavors[:_MAX_PLAN_VM_OPTIONS])
        ]
        questions.append(
            {
                "id": f"q{q_idx}",
                "header": "Flavor",
                "question": "Which flavor (size) should the VM use?",
                "options": flavor_options,
                "multiSelect": False,
                "allowFreeText": False,
            }
        )
    if "image" in missing:
        q_idx = len(questions)
        image_options = [
            {"id": f"q{q_idx}o{i}", "label": img["name"], "value": img["name"]}
            for i, img in enumerate(images[:_MAX_PLAN_VM_OPTIONS])
        ]
        questions.append(
            {
                "id": f"q{q_idx}",
                "header": "Image",
                "question": "Which OS image should the VM use?",
                "options": image_options,
                "multiSelect": False,
                "allowFreeText": False,
            }
        )
    return {
        "type": "success",
        "summary": (
            "Configuration form rendered ("
            + " and ".join(missing)
            + "). STOP — wait for the user's reply. Do NOT call another "
            "tool until they respond."
        ),
        "ui_component": "ask_user_form",
        "ui_data": {"questions": questions},
    }


def format_needs_name() -> dict:
    """ask_user_form free-text question asking for the VM name."""
    return {
        "type": "success",
        "summary": (
            "Name input form rendered. STOP — wait for the user's reply. "
            "Do NOT call another tool until they respond."
        ),
        "ui_component": "ask_user_form",
        "ui_data": {
            "questions": [
                {
                    "id": "q0",
                    "header": "Name",
                    "question": "What should the VM be named?",
                    "multiSelect": False,
                }
            ],
        },
    }
