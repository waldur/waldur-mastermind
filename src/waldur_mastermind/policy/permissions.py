from rest_framework.permissions import BasePermission

from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions


class StaffAndOwnerHaveFullPermissionsProjectTeamOnlyRead(BasePermission):
    def has_permission(self, request, view):
        user = request.user

        if not bool(request.user and request.user.is_authenticated):
            return False

        if user.is_staff:
            return True

        if view.action not in ['update', 'partial_update', 'destroy']:
            return True

        obj = view.get_object()
        customer = structure_permissions._get_customer(obj)

        if structure_models.CustomerPermission.objects.filter(
            customer=customer, user=user, is_active=True
        ).exists():
            return True

        return False
