from django.utils.translation import gettext_lazy as _

from waldur_core.core import exceptions as core_exceptions
from waldur_openstack.models import (
    CustomerOpenStack,
    Flavor,
    Image,
    Instance,
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


def get_external_network_id(tenant: Tenant):
    """
    Fetch external network ID from tenant service settings or customer settings.
    """
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
