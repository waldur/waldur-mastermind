import logging

from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import transaction
from rest_framework import exceptions

from waldur_core.structure import models as structure_models
from waldur_core.structure.backend import ServiceBackend
from waldur_mastermind.invoices import utils as invoice_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import plugins
from waldur_mastermind.marketplace.utils import import_resource_metadata
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    INSTANCE_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
    STORAGE_MODE_FIXED,
    STORAGE_TYPE,
    TENANT_TYPE,
    VOLUME_TYPE,
)
from waldur_openstack.openstack import models as openstack_models

logger = logging.getLogger(__name__)
TenantQuotas = openstack_models.Tenant.Quotas


def get_offering_category_for_tenant():
    return marketplace_models.Category.objects.get(default_tenant_category=True)


def get_offering_name_for_instance(tenant):
    return 'Virtual machine in %s' % tenant.name


def get_offering_category_for_instance():
    return marketplace_models.Category.objects.get(default_vm_category=True)


def get_offering_name_for_volume(tenant):
    return 'Volume in %s' % tenant.name


def get_offering_category_for_volume():
    return marketplace_models.Category.objects.get(default_volume_category=True)


def get_category_and_name_for_offering_type(offering_type, service_settings):
    if offering_type == INSTANCE_TYPE:
        category = get_offering_category_for_instance()
        name = get_offering_name_for_instance(service_settings)
        return category, name
    elif offering_type == VOLUME_TYPE:
        category = get_offering_category_for_volume()
        name = get_offering_name_for_volume(service_settings)
        return category, name


def create_offering_components(offering):
    fixed_components = plugins.manager.get_components(TENANT_TYPE)

    for component_data in fixed_components:
        marketplace_models.OfferingComponent.objects.create(
            offering=offering, **component_data._asdict()
        )


def import_volume_metadata(resource):
    import_resource_metadata(resource)
    volume = resource.scope
    resource.backend_metadata['size'] = volume.size

    if volume.instance:
        resource.backend_metadata['instance_uuid'] = volume.instance.uuid.hex
        resource.backend_metadata['instance_name'] = volume.instance.name
    else:
        resource.backend_metadata['instance_uuid'] = None
        resource.backend_metadata['instance_name'] = None

    if volume.type:
        resource.backend_metadata['type_name'] = volume.type.name
    else:
        resource.backend_metadata['type_name'] = None

    resource.save(update_fields=['backend_metadata'])


def import_instance_metadata(resource):
    import_resource_metadata(resource)
    instance = resource.scope
    resource.backend_metadata['internal_ips'] = instance.internal_ips
    resource.backend_metadata['external_ips'] = instance.external_ips
    resource.save(update_fields=['backend_metadata'])


def get_offering(offering_type, service_settings):
    try:
        return marketplace_models.Offering.objects.get(
            scope=service_settings, type=offering_type
        )
    except ObjectDoesNotExist:
        logger.warning(
            'Marketplace offering is not found. ' 'ServiceSettings ID: %s',
            service_settings.id,
        )
    except MultipleObjectsReturned:
        logger.warning(
            'Multiple marketplace offerings are found. ' 'ServiceSettings ID: %s',
            service_settings.id,
        )


def import_quotas(offering, quotas, field):
    source_values = {row['name']: row[field] for row in quotas.values('name', field)}
    storage_mode = offering.plugin_options.get('storage_mode') or STORAGE_MODE_FIXED

    result_values = {
        CORES_TYPE: source_values.get(TenantQuotas.vcpu.name, 0),
        RAM_TYPE: source_values.get(TenantQuotas.ram.name, 0),
    }

    if storage_mode == STORAGE_MODE_FIXED:
        result_values[STORAGE_TYPE] = source_values.get(TenantQuotas.storage.name, 0)
    elif storage_mode == STORAGE_MODE_DYNAMIC:
        volume_type_values = {
            k: v for (k, v) in source_values.items() if k.startswith('gigabytes_')
        }
        result_values.update(volume_type_values)

    return result_values


def _apply_quotas(target, quotas):
    for name, limit in quotas.items():
        target.set_quota_limit(name, limit)


def import_usage(resource):
    tenant = resource.scope

    if not tenant:
        return

    resource.current_usages = import_quotas(resource.offering, tenant.quotas, 'usage')
    resource.save(update_fields=['current_usages'])


def import_limits(resource):
    """
    Import resource quotas as marketplace limits.
    :param resource: Marketplace resource
    """
    tenant = resource.scope

    if not tenant:
        return

    resource.limits = import_quotas(resource.offering, tenant.quotas, 'limit')
    resource.save(update_fields=['limits'])


def map_limits_to_quotas(limits, offering):
    quotas = {
        TenantQuotas.vcpu.name: limits.get(CORES_TYPE),
        TenantQuotas.ram.name: limits.get(RAM_TYPE),
        TenantQuotas.storage.name: limits.get(STORAGE_TYPE),
    }

    quotas = {k: v for k, v in quotas.items() if v is not None}

    # Filter volume-type quotas.
    volume_type_quotas = dict(
        (key, value)
        for (key, value) in limits.items()
        if key.startswith('gigabytes_') and value is not None
    )

    # Common storage quota should be equal to sum of all volume-type quotas.
    if volume_type_quotas:
        if 'storage' in quotas:
            raise exceptions.ValidationError(
                'You should either specify general-purpose storage quota '
                'or volume-type specific storage quota.'
            )

        # Initialize volume type quotas as zero, otherwise they are treated as unlimited
        for volume_type in openstack_models.VolumeType.objects.filter(
            settings=offering.scope
        ):
            volume_type_quotas.setdefault('gigabytes_' + volume_type.name, 0)

        quotas['storage'] = ServiceBackend.gb2mb(sum(list(volume_type_quotas.values())))
        quotas.update(volume_type_quotas)

    # Convert quota value from float to integer because OpenStack API fails otherwise
    quotas = {k: int(v) for k, v in quotas.items()}

    return quotas


def update_limits(order_item):
    tenant = order_item.resource.scope
    backend = tenant.get_backend()
    quotas = map_limits_to_quotas(order_item.limits, order_item.offering)
    backend.push_tenant_quotas(tenant, quotas)
    with transaction.atomic():
        _apply_quotas(tenant, quotas)
        for target in structure_models.ServiceSettings.objects.filter(scope=tenant):
            _apply_quotas(target, quotas)


def import_limits_when_storage_mode_is_switched(resource):
    tenant = resource.scope

    if not tenant:
        return

    storage_mode = (
        resource.offering.plugin_options.get('storage_mode') or STORAGE_MODE_FIXED
    )

    raw_limits = {quota.name: quota.limit for quota in tenant.quotas.all()}
    raw_usages = {quota.name: quota.usage for quota in tenant.quotas.all()}

    limits = {
        CORES_TYPE: raw_limits.get(TenantQuotas.vcpu.name, 0),
        RAM_TYPE: raw_limits.get(TenantQuotas.ram.name, 0),
    }

    if storage_mode == STORAGE_MODE_FIXED:
        limits[STORAGE_TYPE] = raw_usages.get(TenantQuotas.storage.name, 0)
    elif storage_mode == STORAGE_MODE_DYNAMIC:
        volume_type_limits = {
            k: v for (k, v) in raw_usages.items() if k.startswith('gigabytes_')
        }
        limits.update(volume_type_limits)

    resource.limits = limits
    resource.save(update_fields=['limits'])


def push_tenant_limits(resource):
    tenant = resource.scope
    backend = tenant.get_backend()
    quotas = map_limits_to_quotas(resource.limits, resource.offering)
    backend.push_tenant_quotas(tenant, quotas)
    with transaction.atomic():
        _apply_quotas(tenant, quotas)
        for target in structure_models.ServiceSettings.objects.filter(scope=tenant):
            _apply_quotas(target, quotas)


def restore_limits(resource):
    order_item = (
        marketplace_models.OrderItem.objects.filter(
            resource=resource,
            type__in=[
                marketplace_models.OrderItem.Types.CREATE,
                marketplace_models.OrderItem.Types.UPDATE,
            ],
        )
        .order_by('-created')
        .first()
    )

    if not order_item:
        return

    if not isinstance(order_item.resource.scope, openstack_models.Tenant):
        return

    update_limits(order_item)


def serialize_resource_limit_period(period):
    billing_periods = invoice_utils.get_full_days(period['start'], period['end'])
    return {
        'start': period['start'].isoformat(),
        'end': period['end'].isoformat(),
        'quantity': period['quantity'],
        'billing_periods': billing_periods,
        'total': str(period['quantity'] * billing_periods),
    }
