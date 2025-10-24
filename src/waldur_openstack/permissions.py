from django.conf import settings
from rest_framework import exceptions

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission


def _check_permissions(permission):
    """Creates a permission check function for OpenStack instance operations.

    Args:
        permission: The permission enum to check for

    Returns:
        A function that validates the permission for the given instance
    """

    def func(request, view, instance=None):
        if not instance:
            return

        if request.user.is_staff:
            return

        if has_permission(request, permission, instance.project):
            return

        if has_permission(request, permission, instance.project.customer):
            return

        raise exceptions.PermissionDenied()

    return func


can_manage_openstack_instance_power = _check_permissions(
    PermissionEnum.CAN_MANAGE_OPENSTACK_INSTANCE_POWER
)

can_manage_openstack_instance = _check_permissions(
    PermissionEnum.CAN_MANAGE_OPENSTACK_INSTANCE
)


def has_permissions_for_console(request, view, instance=None):
    permission = PermissionEnum.HAS_OPENSTACK_INSTANCE_CONSOLE_ACCESS

    if not instance:
        return

    if request.user.is_staff:
        return

    if settings.WALDUR_OPENSTACK["ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS"]:
        if has_permission(request, permission, instance.project):
            return

        if has_permission(request, permission, instance.project.customer):
            return

    raise exceptions.PermissionDenied()
