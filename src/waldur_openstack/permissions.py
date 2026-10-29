from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
    ServiceProviderRole,
)
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


def can_manage_project(user, project):
    """Whether the user is staff, or an owner, admin or manager of the project."""
    if user is None or not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    return (
        project.has_user(user, ProjectRole.ADMIN)
        or project.has_user(user, ProjectRole.MANAGER)
        or project.customer.has_user(user, CustomerRole.OWNER)
    )


def can_manage_network_project(user, network):
    """Whether the user may act as the owner of the network's project."""
    return can_manage_project(user, network.project)


def owns_network_of_port(request, view, port=None):
    """Port settings Neutron leaves to the owner of the port's network.

    A tenant that reaches a network through a share may attach ports to it,
    but only the network's owner may turn their port security off or choose
    their addresses: otherwise one tenant could spoof the others on a shared
    segment. Waldur calls Neutron as the cloud admin, which Neutron does not
    hold to that rule, so it is applied here.
    """
    if not port or not port.network_id or port.network.tenant_id == port.tenant_id:
        return
    if not can_manage_network_project(request.user, port.network):
        raise exceptions.PermissionDenied(
            _(
                "Network %s belongs to another tenant: only its owner can change "
                "the port security or addresses of ports on it."
            )
            % port.network
        )
