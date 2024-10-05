from waldur_openstack.models import Flavor, Image, Tenant, VolumeType


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
