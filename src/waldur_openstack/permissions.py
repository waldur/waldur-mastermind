from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.core import access
from waldur_core.core.permissions import has_staff_access, has_support_access
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ServiceProviderRole,
)
from waldur_core.permissions.utils import check_pat_binding_on_path, has_permission
from waldur_core.structure import permissions as structure_permissions
from waldur_openstack.utils import within_provider_bindings


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

        # Staff act everywhere, but a personal access token only within its
        # bindings, as for the roles below.
        if has_staff_access(request) and check_pat_binding_on_path(
            request, [instance.project]
        ):
            return

        if has_permission(request, permission, instance.project):
            return

        if has_permission(request, permission, instance.project.customer):
            return

        raise exceptions.PermissionDenied()

    return access.describes(
        access.permission(permission, ["project", "project.customer"]),
        needs_object=True,
    )(func)


can_manage_openstack_instance_power = _check_permissions(
    PermissionEnum.CAN_MANAGE_OPENSTACK_INSTANCE_POWER
)

can_manage_openstack_instance = _check_permissions(
    PermissionEnum.CAN_MANAGE_OPENSTACK_INSTANCE
)

can_manage_openstack_router_gateway = _check_permissions(
    PermissionEnum.CAN_MANAGE_OPENSTACK_ROUTER_GATEWAY
)


@access.describes(
    access.custom(
        "Staff; and, while ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS is on, "
        f"holders of {PermissionEnum.HAS_OPENSTACK_INSTANCE_CONSOLE_ACCESS.value} "
        "on the instance's project or organization."
    ),
    needs_object=True,
)
def has_permissions_for_console(request, view, instance=None):
    permission = PermissionEnum.HAS_OPENSTACK_INSTANCE_CONSOLE_ACCESS

    if not instance:
        return

    if has_staff_access(request) and check_pat_binding_on_path(
        request, [instance.project]
    ):
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
    service_settings = getattr(instance.tenant, "service_settings", None)
    customer = getattr(service_settings, "customer", None) if service_settings else None
    if _acts_as_provider(
        request, customer, staff=has_support_access(request), target=instance.project
    ):
        return
    raise exceptions.PermissionDenied()


def _acts_as_provider(request, customer, staff, target=None) -> bool:
    """Whether the request acts for the provider ``customer``: as staff, or as
    an owner or service provider manager of it. A personal access token acts
    only within its bindings (``utils.within_provider_bindings``); ``target``
    is the project acted in."""
    if not (
        staff
        or (
            customer is not None
            and (
                customer.has_user(request.user, CustomerRole.OWNER)
                or customer.has_user(request.user, ServiceProviderRole.MANAGER)
            )
        )
    ):
        return False
    return within_provider_bindings(request, customer, staff, target)


@access.describes(
    access.custom(
        "Staff, or an owner or service provider manager of the organization "
        "that owns the tenant's service settings, that is, the provider. A "
        "personal access token passes only within its bindings."
    ),
    needs_object=True,
)
def can_update_tenant_quotas_as_service_provider(request, view, tenant=None):
    if not tenant:
        return
    service_settings = tenant.service_settings
    customer = service_settings.customer if service_settings else None
    if _acts_as_provider(
        request, customer, staff=has_staff_access(request), target=tenant.project
    ):
        return
    raise exceptions.PermissionDenied()


def can_manage_project(request, project):
    """Whether the request may act for the project: staff, an owner of its
    organization, or an administrator or manager of it.

    It decides as ``is_administrator`` does, so a personal access token
    passes only within its bindings: a token bound to one project must not
    put ports into another project's tenants, or use its networks, with its
    user's roles. Without a request nobody passes.
    """
    if request is None or not request.user.is_authenticated:
        return False
    try:
        structure_permissions.is_administrator(request, None, project)
    except exceptions.PermissionDenied:
        return False
    return True


def can_manage_network_project(request, network):
    """Whether the request may act as the owner of the network's project."""
    return can_manage_project(request, network.project)


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
    if not can_manage_network_project(request, port.network):
        raise exceptions.PermissionDenied(
            _(
                "Network %s belongs to another tenant: only its owner can change "
                "the port security or addresses of ports on it."
            )
            % port.network
        )
