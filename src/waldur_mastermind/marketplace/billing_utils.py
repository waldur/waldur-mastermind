import math
from decimal import Decimal

from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    SLURM_OFFERING,
)
from waldur_mastermind.marketplace.models import (
    OfferingComponent,
    PlanComponent,
    Resource,
)
from waldur_mastermind.marketplace_openstack import CORES_TYPE, RAM_TYPE, STORAGE_TYPE
from waldur_openstack.utils import is_valid_volume_type_name


def convert_slurm_usage(usage: int | float | Decimal, component_type: str) -> int:
    # This is temporarily uplifted to marketplace in order to avoid circular dependency
    minutes_in_hour = 60
    usage_float = float(usage)
    if component_type in ["ram", "mem"]:
        mb_in_gb = 1024
        quantity = int(math.ceil(usage_float / mb_in_gb / minutes_in_hour))
    else:
        quantity = int(math.ceil(usage_float / minutes_in_hour))
    return quantity


def convert_quantity(usage, offering_type, component_type: str, billing_type=None):
    """
    Convert usage quantity for billing purposes.

    Base implementation returns usage as-is. Can be overridden in
    subclasses to apply component-specific conversion factors.

    Args:
        usage: Raw usage quantity
        component_type: Type of component being processed
        billing_type: Optional billing type for precision control.
            When USAGE, returns Decimal instead of int to preserve
            fractional component-hours.

    Returns:
        Converted quantity for billing
    """
    from waldur_mastermind.marketplace.enums import BillingTypes

    if offering_type == SLURM_OFFERING:
        return convert_slurm_usage(usage, component_type)
    if offering_type == OPENSTACK_TENANT_OFFERING:
        if component_type in (STORAGE_TYPE, RAM_TYPE):
            result = Decimal(str(usage)) / 1024
        else:
            result = Decimal(str(usage))
        if billing_type == BillingTypes.USAGE:
            return result.quantize(Decimal("0.01"))
        return int(result)
    return usage


def get_component_name(plan_component: PlanComponent) -> str:
    """
    Get display name for a plan component.

    Args:
        plan_component: Plan component instance

    Returns:
        Component name for display purposes
    """
    if plan_component.component.offering.type == OPENSTACK_TENANT_OFFERING:
        component_type = plan_component.component.type
        if component_type == CORES_TYPE:
            return "CPU"
        elif component_type == RAM_TYPE:
            return "RAM"
        elif component_type == STORAGE_TYPE:
            return "storage"
        elif is_valid_volume_type_name(component_type):
            return f"{component_type.replace('gigabytes_', '')} storage"
    return plan_component.component.name


def get_component_details(
    resource: Resource,
    plan_component: PlanComponent | None,
    offering_component: OfferingComponent | None = None,
    plan=None,
):
    """
    Generate detailed metadata for invoice items.

    Creates comprehensive details object containing resource, plan, offering,
    and component information for invoice line items. This metadata is used
    for reporting, analytics, and invoice item identification.

    Args:
        resource: Marketplace resource being billed
        plan_component: Plan component being billed (can be None if not found)
        offering_component: Offering component (used when plan_component is None)

    Returns:
        Dictionary containing detailed metadata for the invoice item
    """
    customer = resource.offering.customer
    service_provider = getattr(customer, "serviceprovider", None)
    # The plan the item is priced with: after a plan switch, usage accrued
    # under the previous period is still billed by the previous plan.
    if plan is None:
        plan = plan_component.plan if plan_component else resource.plan

    # Determine component details from plan_component or offering_component
    if plan_component:
        component_id = plan_component.id
        component_type = plan_component.component.type
        component_name = plan_component.component.name
    elif offering_component:
        component_id = None
        component_type = offering_component.type
        component_name = offering_component.name
    else:
        component_id = None
        component_type = ""
        component_name = ""

    return {
        "resource_name": resource.name,
        "resource_uuid": resource.uuid.hex,
        "plan_name": plan.name if plan else "",
        "plan_uuid": plan.uuid.hex if plan else "",
        "offering_type": resource.offering.type,
        "offering_name": resource.offering.name,
        "offering_uuid": resource.offering.uuid.hex,
        "service_provider_name": customer.name,
        "service_provider_uuid": ""
        if not service_provider
        else service_provider.uuid.hex,
        "plan_component_id": component_id,
        "offering_component_type": component_type,
        "offering_component_name": component_name,
    }


def get_invoice_item_name(resource: Resource):
    """
    Generate display name for invoice items.

    Creates a descriptive name combining resource name, offering name,
    and plan name (if available) for invoice line items.

    Args:
        resource: Marketplace resource being billed

    Returns:
        String representation for invoice item display
    """
    if resource.plan:
        return f"{resource.name} ({resource.offering.name} / {resource.plan.name})"
    else:
        return f"{resource.name} ({resource.offering.name})"
