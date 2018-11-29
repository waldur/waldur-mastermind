import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack import INSTANCE_TYPE, VOLUME_TYPE, PACKAGE_TYPE
from waldur_mastermind.packages import models as package_models
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps

logger = logging.getLogger(__name__)


def get_offering_name_for_instance(tenant):
    return 'Virtual machine in %s' % tenant.name


def get_offering_name_for_volume(tenant):
    return 'Volume in %s' % tenant.name


def create_missing_offerings(category, tenants=None):
    marketplace_offerings = marketplace_models.Offering.objects.filter(type=INSTANCE_TYPE)
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

        for (offering_type, offering_name) in (
                (INSTANCE_TYPE, get_offering_name_for_instance(tenant)),
                (VOLUME_TYPE, get_offering_name_for_volume(tenant))
        ):
            marketplace_models.Offering.objects.create(
                customer=tenant.customer,
                category=category,
                name=offering_name,
                scope=service_settings,
                shared=False,
                type=offering_type
            )


def create_offering_and_plan_for_package_template(category, customer, template):
    service_settings = template.service_settings

    with transaction.atomic():
        defaults = dict(
            name=service_settings.name,
            geolocations=service_settings.geolocations,
            customer=customer,
            category=category,
        )
        offering, _ = marketplace_models.Offering.objects.get_or_create(
            scope=service_settings,
            type=PACKAGE_TYPE,
            defaults=defaults,
        )
        marketplace_models.Plan.objects.create(
            scope=template,
            offering=offering,
            name=template.name,
            unit_price=template.price,
            unit=marketplace_models.Plan.Units.PER_DAY,
            product_code=template.product_code,
            article_code=template.article_code,
        )


def create_package_missing_offerings(category, customer):
    offerings = marketplace_models.Offering.objects.filter(type=PACKAGE_TYPE)
    front_settings = set(offerings.exclude(object_id=None).values_list('object_id', flat=True))
    back_settings = set(package_models.PackageTemplate.objects.all().values_list('service_settings_id', flat=True))
    missing_ids = back_settings - front_settings

    missing_templates = package_models.PackageTemplate.objects.filter(service_settings__in=missing_ids)
    for template in missing_templates:
        create_offering_and_plan_for_package_template(category, customer, template)
