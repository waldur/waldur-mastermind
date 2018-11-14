import logging

from django.core.exceptions import ObjectDoesNotExist

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_instance import PLUGIN_NAME
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps

logger = logging.getLogger(__name__)


def get_offering_name_for_tenant(tenant):
    return 'Virtual machine in %s' % tenant.name


def create_missing_offerings(category, tenants=None):
    marketplace_offerings = marketplace_models.Offering.objects.filter(type=PLUGIN_NAME)
    front_settings = set(marketplace_offerings.exclude(object_id=None).values_list('object_id', flat=True))
    back_settings = set(structure_models.ServiceSettings.objects.filter(
        type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
    ).exclude(object_id__isnull=True).values_list('id', 'object_id'))

    missing_ids = {b[0] for b in back_settings} - front_settings

    missing_tenants = openstack_models.Tenant.objects.\
        filter(id__in={b[1] for b in back_settings if b[0] in missing_ids})

    if tenants:
        missing_tenants = missing_tenants.filter(uuid__in=tenants)

    for tenant in missing_tenants:
        try:
            service_settings = structure_models.ServiceSettings.objects.get(
                scope=tenant,
                type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
            )
        except ObjectDoesNotExist:
            logger.debug('Skipping offering creation for tenant because service settings '
                         'object does not exist. OpenStack tenant ID: %s', tenant.id)
            continue

        marketplace_models.Offering.objects.create(
            customer=tenant.customer,
            category=category,
            name=get_offering_name_for_tenant(tenant),
            scope=service_settings,
            shared=False,
            type=PLUGIN_NAME
        )
