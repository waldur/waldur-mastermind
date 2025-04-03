import time

from rest_framework import status
from rest_framework.reverse import reverse
from waldur_client import WaldurClientException

from waldur_core.core.utils import get_system_robot
from waldur_mastermind.common.utils import create_request
from waldur_mastermind.marketplace.models import Order, Resource
from waldur_mastermind.marketplace.views import BaseResourceViewSet, OrderViewSet
from waldur_rancher.exceptions import RancherException


def is_order_ready(uuid):
    order = Order.objects.get(uuid=uuid)
    if order.state == Order.States.ERRED:
        raise RancherException("Order is in erred state.")
    return order.state == Order.States.DONE


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


def submit_creation_order(
    user, offering, plan, project, attributes, limits=None
) -> str:
    post_data = {
        "project": reverse("project-detail", kwargs={"uuid": project.uuid.hex}),
        "offering": reverse(
            "marketplace-public-offering-detail",
            kwargs={"uuid": offering.uuid.hex},
        ),
        "plan": reverse(
            "marketplace-public-offering-plan-detail",
            kwargs={"uuid": plan.uuid.hex, "plan_uuid": plan.uuid.hex},
        ),
        "attributes": dict(effective_user_uuid=user.uuid.hex, **attributes),
        "limits": limits,
    }
    view = OrderViewSet.as_view({"post": "create"})
    response = create_request(view, get_system_robot(), post_data)

    if response.status_code != status.HTTP_201_CREATED:
        raise WaldurClientException(response.data)
    order_uuid = response.data["uuid"]
    wait_for_order(order_uuid)
    return order_uuid


def submit_termination_order(resource: Resource):
    view = BaseResourceViewSet.as_view({"post": "terminate"})
    response = create_request(view, get_system_robot(), uuid=resource.uuid.hex)
    if response.status_code != status.HTTP_200_OK:
        raise WaldurClientException(response.data)
    order_uuid = response.data["order_uuid"]
    wait_for_order(order_uuid)
    return order_uuid
