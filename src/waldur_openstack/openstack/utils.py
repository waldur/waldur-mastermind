from django.db.models import Q

from waldur_openstack.openstack.models import Flavor, Image, Tenant, VolumeType


def filter_property_for_tenant(qs, tenant):
    return qs.filter(settings=tenant.service_settings).filter(
        Q(tenants=None) | Q(tenants=tenant)
    )


def is_flavor_valid_for_tenant(flavor: Flavor, tenant: Tenant):
    return (
        filter_property_for_tenant(Flavor.objects.all(), tenant)
        .filter(id=flavor.id)
        .exists()
    )


def is_image_valid_for_tenant(image: Image, tenant: Tenant):
    return (
        filter_property_for_tenant(Image.objects.all(), tenant)
        .filter(id=image.id)
        .exists()
    )


def is_volume_type_valid_for_tenant(volume_type: VolumeType, tenant: Tenant):
    return (
        filter_property_for_tenant(VolumeType.objects.all(), tenant)
        .filter(id=volume_type.id)
        .exists()
    )


def volume_type_name_to_quota_name(volume_type_name):
    return f"gigabytes_{volume_type_name}"


def is_valid_volume_type_name(name):
    return name.startswith("gigabytes_")
