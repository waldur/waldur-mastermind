from rest_framework import exceptions

from waldur_core.core import access
from waldur_core.core.permissions import has_staff_access
from waldur_core.permissions.enums import PermissionEnum
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes
from waldur_mastermind.marketplace.permissions import has_project_permission


@access.describes(
    access.any_of(
        access.owned("The creator of the booking's executing order."),
        access.permission(
            PermissionEnum.REJECT_BOOKING_REQUEST, ["project", "project.customer"]
        ),
    ),
    needs_object=True,
)
def user_can_reject_order(request, view, resource=None):
    """Let staff, the order's creator, or REJECT_BOOKING_REQUEST on the resource's
    project or its organization reject a booking.

    A booking with no executing order, or more than one, passes here; the
    rejection then fails with 400, as there is no single order to cancel.
    """
    if not resource:
        return

    user = request.user

    if has_staff_access(request):
        return

    try:
        order = models.Order.objects.get(
            resource=resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
        )
    except models.Order.DoesNotExist:
        return
    except models.Order.MultipleObjectsReturned:
        return

    if user == order.created_by:
        return

    if has_project_permission(
        request, PermissionEnum.REJECT_BOOKING_REQUEST, resource.project
    ):
        return

    raise exceptions.PermissionDenied()
