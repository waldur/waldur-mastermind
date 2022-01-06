from django.conf import settings as django_settings
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions

from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions

from . import models


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

    if django_settings.WALDUR_MARKETPLACE[
        'OWNER_CAN_APPROVE_ORDER'
    ] and structure_permissions._has_owner_access(user, project.customer):
        return True

    if django_settings.WALDUR_MARKETPLACE[
        'MANAGER_CAN_APPROVE_ORDER'
    ] and structure_permissions._has_manager_access(user, project):
        return True

    if django_settings.WALDUR_MARKETPLACE[
        'ADMIN_CAN_APPROVE_ORDER'
    ] and structure_permissions._has_admin_access(user, project):
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
        if (
            project.get_users(structure_models.ProjectRole.ADMINISTRATOR)
            .filter(pk=user.pk)
            .exists()
        ):
            return
        if (
            project.get_users(structure_models.ProjectRole.MANAGER)
            .filter(pk=user.pk)
            .exists()
        ):
            return

    owned_customers = set(
        structure_models.Customer.objects.all()
        .filter(
            permissions__user=user,
            permissions__is_active=True,
            permissions__role=structure_models.CustomerRole.OWNER,
        )
        .distinct()
    )

    if offering.customer not in owned_customers:
        raise exceptions.PermissionDenied(
            'Import is limited to owners for private offerings.'
        )


def user_can_terminate_resource(request, view, resource=None):
    if not resource:
        return

    # Project manager/admin and customer owner are allowed to terminate resource.
    if structure_permissions._has_admin_access(request.user, resource.project):
        return

    # Service provider is allowed to terminate resource too.
    if structure_permissions._has_owner_access(
        request.user, resource.offering.customer
    ):
        return

    raise exceptions.PermissionDenied()


def user_is_owner_or_service_manager(request, view, obj=None):
    if not obj:
        return

    if isinstance(obj, models.Offering):
        offering = obj
    elif isinstance(obj, models.Resource):
        customer = structure_permissions._get_customer(obj)

        if structure_permissions._has_owner_access(request.user, customer):
            return

        offering = obj.offering
    else:
        return

    if offering.has_user(request.user):
        return

    if structure_permissions._has_owner_access(request.user, offering.customer):
        return

    if offering.customer.has_user(
        request.user, role=structure_models.CustomerRole.SERVICE_MANAGER
    ):
        return

    raise exceptions.PermissionDenied()


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
    if request.user.is_support:
        return
    else:
        user_is_service_provider_owner_or_service_provider_manager(request, view, obj)


def can_approve_order_item(request, view, obj=None):
    if not obj:
        return

    if structure_permissions._has_owner_access(request.user, obj.offering.customer):
        return

    raise exceptions.PermissionDenied()


def can_reject_order_item(request, view, obj=None):
    if not obj:
        return

    if structure_permissions._has_owner_access(
        request.user, obj.order.project.customer
    ):
        return

    if structure_permissions._has_owner_access(request.user, obj.offering.customer):
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
        if structure_permissions._has_owner_access(request.user, offering.customer):
            return

        if offering.customer.has_user(
            request.user, role=structure_models.CustomerRole.SERVICE_MANAGER
        ):
            return

    raise exceptions.PermissionDenied()
