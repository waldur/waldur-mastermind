from rest_framework import permissions

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission, permission_factory

user_can_accept_requested_offering = permission_factory(
    PermissionEnum.ACCEPT_REQUESTED_OFFERING,
    ["offering.customer"],
)


class CanUpdateCallPermission(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        call = obj.call
        return has_permission(
            request,
            PermissionEnum.UPDATE_CALL,
            call,
        )
