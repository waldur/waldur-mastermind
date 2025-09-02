import logging
import time
from typing import cast

from django.db.models import Sum
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.reverse import reverse

from waldur_core.core.enums import CoreStates
from waldur_core.core.models import User
from waldur_core.core.utils import get_system_robot
from waldur_core.structure.models import Project
from waldur_mastermind.common.utils import create_request
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OPENSTACK_TENANT_OFFERING, OrderStates
from waldur_mastermind.marketplace.models import Offering, Order, Plan, Resource
from waldur_mastermind.marketplace.views import (
    BaseResourceViewSet,
    ConsumerResourceViewSet,
    OrderViewSet,
)
from waldur_openstack.models import Tenant
from waldur_rancher.exceptions import RancherException
from waldur_rancher.models import Cluster

logger = logging.getLogger(__name__)


def is_order_ready(uuid):
    order = Order.objects.get(uuid=uuid)
    if order.state == OrderStates.ERRED:
        if order.error_message:
            raise RancherException(order.error_message)
        else:
            raise RancherException("Order is in erred state.")
    return order.state == OrderStates.DONE


def wait_for_order(uuid, interval=10, timeout=600):
    ready = is_order_ready(uuid)
    waited = 0
    while not ready:
        time.sleep(interval)
        ready = is_order_ready(uuid)
        waited += interval
        if waited >= timeout:
            raise RancherException(
                f'Order id "{uuid}" has not changed state to stable.'
            )


def is_tenant_ready(uuid):
    tenant = Tenant.objects.get(uuid=uuid)
    if tenant.state == CoreStates.ERRED:
        raise RancherException("Tenant is in erred state.")
    return tenant.state == CoreStates.OK


def wait_for_tenant(uuid, interval=10, timeout=600):
    ready = is_tenant_ready(uuid)
    waited = 0
    while not ready:
        time.sleep(interval)
        ready = is_tenant_ready(uuid)
        waited += interval
        if waited >= timeout:
            raise RancherException(f'Tenant "{uuid}" has not changed state to stable.')


def submit_creation_order(
    user: User,
    offering: Offering,
    plan: Plan | None,
    project: Project,
    attributes,
    limits=None,
    order_wait_timeout=600,
) -> str:
    post_data = {
        "project": reverse("project-detail", kwargs={"uuid": project.uuid.hex}),
        "offering": reverse(
            "marketplace-public-offering-detail",
            kwargs={"uuid": offering.uuid.hex},
        ),
        "attributes": dict(effective_user_uuid=user.uuid.hex, **attributes),
    }
    if plan:
        post_data["plan"] = reverse(
            "marketplace-public-offering-plan-detail",
            kwargs={"uuid": plan.uuid.hex, "plan_uuid": plan.uuid.hex},
        )

    if limits:
        post_data["limits"] = limits

    view = OrderViewSet.as_view({"post": "create"})
    response = create_request(view, get_system_robot(), post_data)
    data = cast(dict, response.data)

    if response.status_code != status.HTTP_201_CREATED:
        raise ValidationError(data)
    order_uuid = data["uuid"]
    wait_for_order(order_uuid, timeout=order_wait_timeout)
    return order_uuid


def submit_termination_order(resource: Resource):
    view = BaseResourceViewSet.as_view({"post": "terminate"})
    response = create_request(
        view, get_system_robot(), uuid=resource.uuid.hex, post_data={}
    )
    data = cast(dict, response.data)
    if response.status_code != status.HTTP_200_OK:
        raise ValidationError(data)
    order_uuid = data["order_uuid"]
    wait_for_order(order_uuid)
    return order_uuid


def submit_update_order(resource: Resource, new_limits: dict):
    post_data = {"limits": new_limits}
    view = ConsumerResourceViewSet.as_view({"post": "update_limits"})
    response = create_request(
        view, get_system_robot(), uuid=resource.uuid.hex, post_data=post_data
    )
    data = cast(dict, response.data)
    if response.status_code != status.HTTP_200_OK:
        raise ValidationError(data)
    order_uuid = data["order_uuid"]
    wait_for_order(order_uuid)
    return order_uuid


def sync_managed_rancher_invoice_items(
    upstream_invoice_item: InvoiceItem, downstream_invoice_item: InvoiceItem
):
    downstream_invoice_item.details = upstream_invoice_item.details
    downstream_invoice_item.quantity = upstream_invoice_item.quantity
    downstream_invoice_item.save(update_fields=["details", "quantity"])


def sync_aggregated_invoice_item(
    upstream_invoice_item: InvoiceItem, downstream_invoice_item: InvoiceItem
):
    """
    Synchronizes aggregated invoice items for managed Rancher resources when upstream OpenStack items change.

    This function recalculates and updates the total quantity for aggregated invoice items
    that represent the sum of resources from all linked OpenStack tenants in a Rancher cluster.

    When an OpenStack tenant's invoice item is updated (e.g., CPU cores or memory usage changes),
    this function ensures that the corresponding aggregated item in the managed Rancher resource
    reflects the new total across all tenants in the cluster.

    Args:
        upstream_invoice_item (InvoiceItem): The updated OpenStack tenant invoice item that triggered the sync
        downstream_invoice_item (InvoiceItem): The corresponding copied invoice item in managed Rancher

    Example:
        If a cluster has 2 OpenStack tenants:
        - Tenant A: 4 CPU cores
        - Tenant B: 6 CPU cores

        The aggregated item will show: 10 CPU cores total

        If Tenant A is updated to 8 cores, this function will:
        - Recalculate: 8 + 6 = 14 cores
        - Update the aggregated item to 14 cores
    """
    managed_rancher_resource = downstream_invoice_item.resource
    if not managed_rancher_resource:
        return

    rancher_resource = cast(
        marketplace_models.Resource | None, managed_rancher_resource.scope
    )
    if not rancher_resource:
        return

    rancher_cluster = cast(Cluster | None, rancher_resource.scope)
    if not rancher_cluster:
        return

    # Extract the component type (e.g., 'cores', 'memory', 'storage') from the upstream item
    # This determines which aggregated item needs to be updated
    offering_component_type = upstream_invoice_item.details.get(
        "offering_component_type"
    )
    if not offering_component_type:
        return

    plan = managed_rancher_resource.plan
    if not plan:
        return

    # Locate the aggregated invoice item for this component type
    # Aggregated items have backend_uuid=None (not copied from OpenStack)
    # and represent the sum of all tenant resources of this type
    try:
        aggregated_invoice_item = InvoiceItem.objects.get(
            resource=managed_rancher_resource,
            invoice=downstream_invoice_item.invoice,
            details__offering_component_type=offering_component_type,
            backend_uuid__isnull=True,
        )
    except (InvoiceItem.DoesNotExist, InvoiceItem.MultipleObjectsReturned):
        return

    # Fetch all invoice items from all tenants linked to this cluster
    # for the same component type and billing period
    component_items_from_all_tenants = InvoiceItem.objects.filter(
        resource__object_id__in=rancher_cluster.linked_tenant_ids,
        resource__offering__type=OPENSTACK_TENANT_OFFERING,
        invoice=upstream_invoice_item.invoice,
        details__offering_component_type=offering_component_type,
    )

    if not component_items_from_all_tenants:
        logger.debug(
            "Skipping aggregate invoice item update for resource %s because no source items are found for offering component %s.",
            upstream_invoice_item.resource,
            offering_component_type,
        )

    # Calculate the total quantity across all tenants
    total_quantity = component_items_from_all_tenants.aggregate(total=Sum("quantity"))[
        "total"
    ]
    aggregated_invoice_item.quantity = total_quantity
    aggregated_invoice_item.save(update_fields=["quantity"])
