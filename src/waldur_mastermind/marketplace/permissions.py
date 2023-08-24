from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission, permission_factory
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions

from . import models


def can_register_service_provider(request, customer):
    if request.user.is_staff:
        return

    if has_permission(request.user, PermissionEnum.REGISTER_SERVICE_PROVIDER, customer):
        return

    raise exceptions.PermissionDenied()


def has_project_permission(user, permission, project):
    return has_permission(user, permission, project) or has_permission(
        user, permission, project.customer
    )


def check_availability_of_auto_approving(items, user, project):
    if user.is_staff:
        return True

    # Skip approval of private offering for project users
    if all(item.offering.is_private for item in items):
        return has_project_permission(
            user, PermissionEnum.APPROVE_PRIVATE_ORDER, project
        )

    # Skip approval of public offering belonging to the same organization under which the request is done
    if all(
        item.offering.shared
        and item.offering.customer == project.customer
        and item.offering.plugin_options.get(
            'auto_approve_in_service_provider_projects'
        )
        is True
        for item in items
    ):
        return True

    # Service provider is not required to approve termination order
    if (
        len(items) == 1
        and items[0].type == models.OrderItem.Types.TERMINATE
        and structure_permissions._has_owner_access(user, items[0].offering.customer)
    ):
        return True

    return has_project_permission(user, PermissionEnum.APPROVE_ORDER, project)


def user_can_approve_order_permission(request, view, order=None):
    if not order:
        return

    user = request.user
    if has_project_permission(user, PermissionEnum.APPROVE_ORDER, order.project):
        return

    raise exceptions.PermissionDenied()


def user_can_reject_order(request, view, order=None):
    if not order:
        return

    user = request.user

    if user.is_staff:
        return

    if user == order.created_by:
        return

    if has_project_permission(user, PermissionEnum.REJECT_ORDER, order.project):
        return

    raise exceptions.PermissionDenied()


def user_can_list_importable_resources(request, view, offering=None):
    if not offering:
        return

    user = request.user
    if user.is_staff:
        return

    if offering.shared:
        raise exceptions.PermissionDenied(
            'Import is limited to staff for shared offerings.'
        )

    # Import private offerings must be available for admins and managers
    if offering.scope and offering.scope.scope and offering.scope.scope.project:
        project = offering.scope.scope.project
        if has_permission(user, PermissionEnum.LIST_IMPORTABLE_RESOURCES, project):
            return

    if not has_permission(
        user, PermissionEnum.LIST_IMPORTABLE_RESOURCES, offering.customer
    ):
        raise exceptions.PermissionDenied()


# Project manager/admin and customer owner are allowed to terminate resource.
# Service provider is allowed to terminate resource too.
user_can_terminate_resource = permission_factory(
    PermissionEnum.TERMINATE_RESOURCE,
    ['project', 'project.customer', 'offering.customer'],
)

user_can_manage_offering_user_group = permission_factory(
    PermissionEnum.MANAGE_OFFERING_USER_GROUP,
    ['offering.customer'],
)


def user_is_service_provider_owner_or_service_provider_manager(request, view, obj=None):
    if not obj:
        return

    if structure_permissions._has_owner_access(request.user, obj.offering.customer):
        return

    if obj.offering.customer.has_user(
        request.user, role=structure_models.CustomerRole.SERVICE_MANAGER
    ):
        return

    raise exceptions.PermissionDenied()


def user_can_set_end_date_by_provider(request, view, obj=None):
    if not obj:
        return
    if request.user.is_support:
        return
    if has_permission(
        request.user, PermissionEnum.SET_RESOURCE_END_DATE, obj.offering.customer
    ):
        return
    raise exceptions.PermissionDenied()


def user_can_update_thumbnail(request, view, obj=None):
    if not obj:
        return

    offering = obj

    if request.user.is_staff:
        return

    if offering.state not in (
        models.Offering.States.ACTIVE,
        models.Offering.States.DRAFT,
        models.Offering.States.PAUSED,
    ):
        raise exceptions.PermissionDenied(_('You are not allowed to update a logo.'))
    else:
        if has_permission(
            request.user, PermissionEnum.UPDATE_OFFERING_THUMBNAIL, offering.customer
        ):
            return

    raise exceptions.PermissionDenied()
