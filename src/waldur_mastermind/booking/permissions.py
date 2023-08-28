from rest_framework import exceptions

from waldur_core.permissions.enums import PermissionEnum
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.permissions import has_project_permission


def user_can_reject_order(request, view, resource=None):
    if not resource:
        return

    user = request.user

    if user.is_staff:
        return

    try:
        order_item = models.OrderItem.objects.get(
            resource=resource,
            type=models.RequestTypeMixin.Types.CREATE,
            state=models.OrderItem.States.EXECUTING,
        )
    except models.OrderItem.DoesNotExist:
        return
    except models.OrderItem.MultipleObjectsReturned:
        return

    if user == order_item.order.created_by:
        return

    if has_project_permission(
        request, PermissionEnum.REJECT_BOOKING_REQUEST, resource.project
    ):
        return

    raise exceptions.PermissionDenied()
