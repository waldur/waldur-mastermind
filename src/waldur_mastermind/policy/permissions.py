from rest_framework.permissions import BasePermission

from waldur_core.permissions.utils import has_user


class StaffAndOwnerHaveFullPermissionsProjectTeamOnlyRead(BasePermission):
    def has_permission(self, request, view):
        user = request.user

        if not bool(request.user and request.user.is_authenticated):
            return False

        if user.is_staff:
            return True

        if view.action not in ["update", "partial_update", "destroy"]:
            return True

        customer = view.get_object().project.customer

        return has_user(customer, user)
