import time
from typing import cast

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.reverse import reverse

from waldur_core.core.enums import CoreStates
from waldur_core.core.models import User
from waldur_core.core.utils import get_system_robot
from waldur_core.structure.models import Project
from waldur_mastermind.common.utils import create_request
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.models import Offering, Order, Plan, Resource
from waldur_mastermind.marketplace.views import BaseResourceViewSet, OrderViewSet
from waldur_openstack.models import Tenant
from waldur_rancher.exceptions import RancherException


def is_order_ready(uuid):
    order = Order.objects.get(uuid=uuid)
    if order.state == OrderStates.ERRED:
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
