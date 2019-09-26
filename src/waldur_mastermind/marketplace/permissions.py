from django.conf import settings as django_settings
from rest_framework import exceptions

from waldur_core.structure import permissions as structure_permissions


def can_register_service_provider(request, customer):
    if request.user.is_staff:
        return
    if not django_settings.WALDUR_MARKETPLACE['OWNER_CAN_REGISTER_SERVICE_PROVIDER']:
        raise exceptions.PermissionDenied()
    structure_permissions.is_owner(request, None, customer)


def check_availability_of_auto_approving(items, user, project):
    if user.is_staff:
        return True

    # Skip approval of private offering for project users
    if all(item.offering.is_private for item in items):
        return structure_permissions._has_admin_access(user, project)

    return user_can_approve_order(user, project)


def user_can_approve_order_permission(request, view, order=None):
    if not order:
        return

    user = request.user
    if user_can_approve_order(user, order.project):
        return

    raise exceptions.PermissionDenied()


def user_can_approve_order(user, project):
    if user.is_staff:
        return True

    if django_settings.WALDUR_MARKETPLACE['OWNER_CAN_APPROVE_ORDER'] and \
            structure_permissions._has_owner_access(user, project.customer):
        return True

    if django_settings.WALDUR_MARKETPLACE['MANAGER_CAN_APPROVE_ORDER'] and \
            structure_permissions._has_manager_access(user, project):
        return True

    if django_settings.WALDUR_MARKETPLACE['ADMIN_CAN_APPROVE_ORDER'] and \
            structure_permissions._has_admin_access(user, project):
        return True

    return False


def user_can_reject_order(request, view, order=None):
    if not order:
        return

    user = request.user

    if user.is_staff:
        return

    if user == order.created_by:
        return

    if structure_permissions._has_admin_access(user, order.project):
        return

    raise exceptions.PermissionDenied()
