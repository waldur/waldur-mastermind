"""Order history for demo preset resources.

In Waldur a resource comes into being through a CREATE order, changes
through UPDATE orders and goes through a TERMINATE order; the resource's
Order history tab reads exactly those. A preset authors resources directly,
and only the few orders a scenario needs, so most demo resources had no
history at all -- a state production never reaches.

This fills in what the lifecycle implies, for every resource the preset left
without it, and leaves orders the preset authored alone:

* every resource gets the CREATE order that made it: done for a resource
  that came up, still executing for one being created, erred for one that
  failed to;
* a terminated resource gets the TERMINATE order that ended it, done; a
  terminating one gets it executing;
* an updating resource gets the UPDATE order it waits on, executing.

Orders are written as the order flow leaves them -- placed by someone on the
project, approved by the consumer and, where the offering asks for it, by the
provider, with their timestamps -- but without replaying the flow's side
effects: the resources are already in the state the preset gave them.
"""

import datetime
import logging

from django.db import transaction
from django.utils import timezone

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    OrderStates,
    OrderTypes,
    ResourceStates,
)

logger = logging.getLogger(__name__)

# How long after it was placed a generated order is approved and completed.
APPROVAL_DELAY = datetime.timedelta(minutes=30)
COMPLETION_DELAY = datetime.timedelta(hours=2)

CREATE_STATE_BY_RESOURCE_STATE = {
    ResourceStates.CREATING: OrderStates.EXECUTING,
    ResourceStates.ERRED: OrderStates.ERRED,
}


def _first(users):
    return next(iter(users), None)


def _consumer(resource):
    """Who placed and approved the resource's orders: someone on the project,
    else an owner of its organisation."""
    project = resource.project
    for role in (
        RoleEnum.PROJECT_MANAGER,
        # The roles of a project awarded through a call.
        RoleEnum.PROJECT_PRINCIPAL_INVESTIGATOR,
        RoleEnum.PROJECT_ADMIN,
        RoleEnum.PROJECT_CO_INVESTIGATOR,
        RoleEnum.PROJECT_MEMBER,
    ):
        user = _first(get_users(project, role_name=role).order_by("date_joined"))
        if user is not None:
            return user
    return _first(
        get_users(project.customer, role_name=RoleEnum.CUSTOMER_OWNER).order_by(
            "date_joined"
        )
    )


def _provider(resource):
    """Who approved for the provider: a manager of the offering, else an
    owner of its organisation."""
    offering = resource.offering
    user = _first(
        get_users(offering, role_name=RoleEnum.OFFERING_MANAGER).order_by("date_joined")
    )
    if user is not None or offering.customer is None:
        return user
    return _first(
        get_users(offering.customer, role_name=RoleEnum.CUSTOMER_OWNER).order_by(
            "date_joined"
        )
    )


def _order(resource, order_type, state, placed_at, consumer, provider):
    order = models.Order(
        type=order_type,
        state=state,
        resource=resource,
        project=resource.project,
        offering=resource.offering,
        plan=resource.plan,
        attributes=resource.attributes if order_type == OrderTypes.CREATE else {},
        limits=resource.limits if order_type != OrderTypes.TERMINATE else {},
        created_by=consumer,
        consumer_reviewed_by=consumer,
        consumer_reviewed_at=placed_at + APPROVAL_DELAY,
        provider_reviewed_by=provider,
        provider_reviewed_at=(placed_at + APPROVAL_DELAY) if provider else None,
        completed_at=(
            placed_at + COMPLETION_DELAY if state == OrderStates.DONE else None
        ),
    )
    order.created = placed_at
    order.modified = order.completed_at or placed_at + APPROVAL_DELAY
    if order_type != OrderTypes.TERMINATE:
        try:
            order.init_cost()
        except Exception as e:  # A plan the estimate cannot price.
            logger.debug("No cost estimate for %s: %s", resource, e)
    return order


def generate_order_history(output=None) -> int:
    """Give every resource the orders its state implies; returns how many
    were added."""
    now = timezone.now()
    orders = []
    resources = models.Resource.objects.select_related(
        "project__customer", "offering__customer", "plan"
    ).prefetch_related("order_set")
    for resource in resources:
        types = {order.type for order in resource.order_set.all()}
        consumer = _consumer(resource)
        provider = _provider(resource)
        created_at = resource.created or now
        if OrderTypes.CREATE not in types:
            orders.append(
                _order(
                    resource,
                    OrderTypes.CREATE,
                    CREATE_STATE_BY_RESOURCE_STATE.get(
                        resource.state, OrderStates.DONE
                    ),
                    created_at - COMPLETION_DELAY,
                    consumer,
                    provider,
                )
            )
        if (
            resource.state in (ResourceStates.TERMINATED, ResourceStates.TERMINATING)
            and OrderTypes.TERMINATE not in types
        ):
            ended_at = max(resource.modified or now, created_at + COMPLETION_DELAY)
            orders.append(
                _order(
                    resource,
                    OrderTypes.TERMINATE,
                    OrderStates.DONE
                    if resource.state == ResourceStates.TERMINATED
                    else OrderStates.EXECUTING,
                    ended_at - COMPLETION_DELAY,
                    consumer,
                    provider,
                )
            )
        if resource.state == ResourceStates.UPDATING and OrderTypes.UPDATE not in types:
            orders.append(
                _order(
                    resource,
                    OrderTypes.UPDATE,
                    OrderStates.EXECUTING,
                    max(now - APPROVAL_DELAY, created_at),
                    consumer,
                    provider,
                )
            )
    with transaction.atomic():
        for order in orders:
            # One at a time, so each slug is made unique against the orders
            # written before it, as the order flow makes it.
            order.slug = order.generate_slug()
            # Written as the flow leaves them, without replaying its side
            # effects on resources already in their final state.
            models.Order.objects.bulk_create([order])
    if output is not None:
        output.write(f"Generated {len(orders)} orders for resource histories\n")
    return len(orders)
