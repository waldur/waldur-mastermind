from django.conf import settings
from rest_framework import exceptions

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
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

can_manage_openstack_router_gateway = _check_permissions(
    PermissionEnum.CAN_MANAGE_OPENSTACK_ROUTER_GATEWAY
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


def can_diagnose_openstack_instance(request, view, instance=None):
    """Gate provider-side diagnostic endpoints on an instance.

    Allows staff, support, and service-provider owners (CUSTOMER.OWNER or
    SERVICE_PROVIDER.MANAGER on the OpenStack ServiceSettings' customer).
    Project-level roles (admin/manager/member) are rejected: the data
    these endpoints expose (Placement resource_provider UUIDs/names,
    fleet topology) is sysadmin-scope, not end-user-scope.
    """
    if not instance:
        return
    user = request.user
    if user.is_staff or user.is_support:
        return
    service_settings = getattr(instance.tenant, "service_settings", None)
    customer = getattr(service_settings, "customer", None) if service_settings else None
    if customer is not None and (
        customer.has_user(user, CustomerRole.OWNER)
        or customer.has_user(user, ServiceProviderRole.MANAGER)
    ):
        return
    raise exceptions.PermissionDenied()


def can_update_tenant_quotas_as_service_provider(request, view, tenant=None):
    if not tenant:
        return

    if request.user.is_staff:
        return

    service_settings = tenant.service_settings
    if not service_settings or not service_settings.customer:
        raise exceptions.PermissionDenied()

    customer = service_settings.customer
    if customer.has_user(request.user, CustomerRole.OWNER) or customer.has_user(
        request.user, ServiceProviderRole.MANAGER
    ):
        return

    raise exceptions.PermissionDenied()
