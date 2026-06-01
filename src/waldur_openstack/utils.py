from django.utils.translation import gettext_lazy as _

from waldur_core.core import exceptions as core_exceptions
from waldur_core.core.utils import stable_topological_sort
from waldur_core.permissions.fixtures import CustomerRole
from waldur_openstack.models import (
    CustomerOpenStack,
    ExternalNetwork,
    Flavor,
    Image,
    Instance,
    SecurityGroup,
    SecurityGroupRule,
    Tenant,
    VolumeType,
)


def is_flavor_valid_for_tenant(flavor: Flavor, tenant: Tenant):
    return Flavor.objects.filter(tenants=tenant, id=flavor.id).exists()


def is_image_valid_for_tenant(image: Image, tenant: Tenant):
    return Image.objects.filter(tenants=tenant, id=image.id).exists()


def is_volume_type_valid_for_tenant(volume_type: VolumeType, tenant: Tenant):
    return VolumeType.objects.filter(tenants=tenant, id=volume_type.id).exists()


def volume_type_name_to_quota_name(volume_type_name):
    return f"gigabytes_{volume_type_name}"


def is_valid_volume_type_name(name):
    return name.startswith("gigabytes_")


def get_valid_availability_zones(instance):
    """
    Fetch valid availability zones for instance or volume from shared settings.
    """
    return (
        instance.tenant.service_settings.options.get("valid_availability_zones") or {}
    )


def is_openstack_service_provider(user, service_settings) -> bool:
    """User is staff or owner of the customer that owns the OpenStack service settings.

    Provider users can manage provider-internal resources (non-shared externals,
    advanced gateway options); consumer-side users may not.
    """
    if user.is_staff:
        return True
    customer = service_settings.customer
    if customer is None:
        return False
    return customer.has_user(user, CustomerRole.OWNER)


def get_tenant_external_networks(tenant: Tenant, user):
    """Global ExternalNetwork rows usable as gateway by `user` on routers in `tenant`.

    Non-shared external networks are provider-internal (e.g. management or
    upstream-peering pools) and must not be exposed to consumer-side users, even
    if Neutron has synced them into the deployment-wide catalog. Providers
    (staff or service settings customer owners) still see the full set.
    """
    qs = ExternalNetwork.objects.filter(settings=tenant.service_settings)
    if not is_openstack_service_provider(user, tenant.service_settings):
        qs = qs.filter(is_shared=True)
    return qs


def get_external_network(tenant: Tenant) -> ExternalNetwork | None:
    """
    Fetch ExternalNetwork instance for a tenant.
    Priority order:
    1. Tenant's external_network_ref FK (if set)
    2. CustomerOpenStack external_network_ref FK (if exists)
    3. Lookup by service settings external_network_id option
    Falls back to string-based lookup if FK is not set.
    """
    # Priority 1: tenant's own FK
    if tenant.external_network_ref_id:
        return tenant.external_network_ref

    service_settings = tenant.service_settings
    customer = tenant.project.customer

    # Priority 2: CustomerOpenStack FK
    try:
        customer_openstack = CustomerOpenStack.objects.get(
            settings=service_settings, customer=customer
        )
        if customer_openstack.external_network_ref_id:
            return customer_openstack.external_network_ref
    except CustomerOpenStack.DoesNotExist:
        pass

    # Priority 3: fall back to string-based lookup via service settings option
    external_network_id = service_settings.get_option("external_network_id")
    if external_network_id:
        return ExternalNetwork.objects.filter(
            settings=service_settings, backend_id=external_network_id
        ).first()

    return None


def get_external_network_id(tenant: Tenant):
    """
    Fetch external network ID from tenant, service settings or customer settings.
    Priority order:
    1. Tenant's external_network_ref FK backend_id (if set)
    2. Tenant's external_network_id field (if set, legacy)
    3. CustomerOpenStack external_network_id (if exists)
    4. Service settings external_network_id option
    """
    # Try FK-based resolution first
    ext_net = get_external_network(tenant)
    if ext_net:
        return ext_net.backend_id

    # Legacy fallback: direct string fields
    if tenant.external_network_id:
        return tenant.external_network_id

    service_settings = tenant.service_settings
    customer = tenant.project.customer
    external_network_id = service_settings.get_option("external_network_id")

    try:
        customer_openstack = CustomerOpenStack.objects.get(
            settings=service_settings, customer=customer
        )
        external_network_id = customer_openstack.external_network_id
    except CustomerOpenStack.DoesNotExist:
        pass
    return external_network_id


def check_volume_resize_enabled(volume):
    if volume.service_settings.options.get("live_resize_of_volumes_enabled", False):
        return

    if volume.bootable:
        raise core_exceptions.IncorrectStateException(_("Volume cannot be bootable."))

    if (
        volume.instance
        and volume.instance.runtime_state != Instance.RuntimeStates.SHUTOFF
    ):
        raise core_exceptions.IncorrectStateException(
            _("Volume instance should be in shutoff state.")
        )


def build_security_groups_dependency_graph(
    security_groups: list[SecurityGroup],
) -> dict[int, set[int]]:
    graph = {sg.id: set() for sg in security_groups}

    rules = SecurityGroupRule.objects.filter(
        security_group__in=security_groups, remote_group__isnull=False
    )
    for rule in rules:
        if rule.remote_group_id != rule.security_group_id:
            graph[rule.security_group_id].add(rule.remote_group_id)

    return graph


def reorder_security_groups_topologically(security_groups: list[SecurityGroup]):
    sg_by_id = {sg.id: sg for sg in security_groups}
    ids = list(sg_by_id.keys())
    graph = build_security_groups_dependency_graph(security_groups)
    sorted_ids = stable_topological_sort(ids, graph)
    return [sg_by_id[i] for i in sorted_ids]
