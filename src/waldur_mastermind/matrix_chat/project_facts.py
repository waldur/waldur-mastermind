"""What bot commands say about a room's project.

Everything here is visible to every member of the room: names and states of
the project's resources and orders, never costs, invoices or credits, and
nothing that depends on who asks.
"""

import dataclasses

from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.models import Order, Resource

ORDER_STATE_LABELS = {
    1: "pending-consumer",
    2: "executing",
    3: "done",
    4: "erred",
    5: "canceled",
    6: "rejected",
    7: "pending-provider",
}

IN_FLIGHT_STATES = (
    (ResourceStates.CREATING, "creating"),
    (ResourceStates.UPDATING, "updating"),
    (ResourceStates.TERMINATING, "terminating"),
)


@dataclasses.dataclass(frozen=True)
class Status:
    errored_total: int
    errored_names: list
    pending_approval: int
    in_flight: dict


def status(project, shown=5):
    """Errored resources, orders waiting for approval, operations in flight."""
    errored_qs = Resource.objects.filter(project=project, state=ResourceStates.ERRED)
    return Status(
        errored_total=errored_qs.count(),
        errored_names=list(errored_qs.values_list("name", flat=True)[:shown]),
        pending_approval=Order.objects.filter(
            project=project,
            state__in=[OrderStates.PENDING_CONSUMER, OrderStates.PENDING_PROVIDER],
        ).count(),
        in_flight={
            label: Resource.objects.filter(project=project, state=state).count()
            for state, label in IN_FLIGHT_STATES
        },
    )


def recent_orders(project, limit=5, states=None):
    orders = Order.objects.filter(project=project).select_related("resource")
    if states is not None:
        orders = orders.filter(state__in=states)
    return list(orders.order_by("-created")[:limit])


def order_state_label(order):
    return ORDER_STATE_LABELS.get(order.state, f"unknown({order.state})")


def resources(project, limit=50):
    """The project's resources that are not terminated, newest first; all of
    them when ``limit`` is None."""
    queryset = (
        Resource.objects.filter(project=project)
        .exclude(state=ResourceStates.TERMINATED)
        .select_related("offering")
        .order_by("-created")
    )
    return list(queryset if limit is None else queryset[:limit])


def has_resource(project, resource_uuid):
    """Whether ``resource_uuid`` is a resource of the project that is not
    terminated."""
    return (
        Resource.objects.filter(project=project, uuid=resource_uuid)
        .exclude(state=ResourceStates.TERMINATED)
        .exists()
    )
