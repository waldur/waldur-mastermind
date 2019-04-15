import decimal
import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack import RAM_TYPE, STORAGE_TYPE
from waldur_mastermind.packages import models as package_models
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.apps import OpenStackConfig
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps
from waldur_openstack.openstack_tenant import models as openstack_tenant_models

from . import INSTANCE_TYPE, PACKAGE_TYPE, VOLUME_TYPE, utils

logger = logging.getLogger(__name__)


def create_template_for_plan(sender, instance, created=False, **kwargs):
    plan = instance

    if plan.scope:
        return

    if not created:
        return

    if plan.offering.type != PACKAGE_TYPE:
        return

    if not isinstance(plan.offering.scope, structure_models.ServiceSettings):
        logger.warning('Skipping plan synchronization because offering scope is not service settings. '
                       'Plan ID: %s', plan.id)
        return

    if plan.offering.scope.type != OpenStackConfig.service_name:
        logger.warning('Skipping plan synchronization because service settings type is not OpenStack. '
                       'Plan ID: %s', plan.id)
        return

    with transaction.atomic():
        template = package_models.PackageTemplate.objects.create(
            service_settings=plan.offering.scope,
            name=plan.name,
            description=plan.description,
            product_code=plan.product_code,
            article_code=plan.article_code,
        )
        plan.scope = template
        plan.save()


def synchronize_plan_component(sender, instance, created=False, **kwargs):
    component = instance

    if not created:
        return

    if component.plan.offering.type != PACKAGE_TYPE:
        return

    template = component.plan.scope
    if not template:
        logger.warning('Skipping plan component synchronization because offering does not have scope. '
                       'Offering ID: %s', component.plan.offering.id)
        return

    if not package_models.PackageComponent.objects.filter(
            template=template, type=component.component.type).exists():

        amount = component.amount
        price = component.price

        # In marketplace RAM and storage is stored in GB, but in package plugin it is stored in MB.
        if component.component.type in (RAM_TYPE, STORAGE_TYPE):
            amount = amount * 1024
            price = decimal.Decimal(price) / decimal.Decimal(1024.0)

        package_models.PackageComponent.objects.create(
            template=template,
            type=component.component.type,
            amount=amount,
            price=price,
        )


def create_offering_from_tenant(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.tracker.has_changed('state'):
        return

    if instance.tracker.previous('state') != instance.States.CREATING:
        return

    if instance.state != instance.States.OK:
        return

    if not settings.WALDUR_MARKETPLACE_OPENSTACK['AUTOMATICALLY_CREATE_PRIVATE_OFFERING']:
        return

    tenant = instance

    try:
        resource = marketplace_models.Resource.objects.get(scope=tenant)
    except ObjectDoesNotExist:
        logger.debug('Skipping offering creation for tenant because order '
                     'item does not exist. OpenStack tenant ID: %s', tenant.id)
        return

    try:
        service_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
        )
    except ObjectDoesNotExist:
        logger.debug('Skipping offering creation for tenant because service settings '
                     'object does not exist. OpenStack tenant ID: %s', tenant.id)
        return

    parent_offering = resource.offering
    for offering_type in (INSTANCE_TYPE, VOLUME_TYPE):
        try:
            category, offering_name = utils.get_category_and_name_for_offering_type(
                offering_type, service_settings)
        except ObjectDoesNotExist:
            logger.warning('Skipping offering creation for tenant because category '
                           'for instances and volumes is not yet defined.')
            continue
        payload = dict(
            type=offering_type,
            name=offering_name,
            scope=service_settings,
            shared=False,
            category=category,
            # OpenStack instance and volume offerings are charged as a part of its tenant
            billable=False,
        )

        fields = (
            'state',
            'customer',
            'attributes',
            'thumbnail',
            'vendor_details',
            'geolocations',
        )
        for field in fields:
            payload[field] = getattr(parent_offering, field)

        with transaction.atomic():
            offering = marketplace_models.Offering.objects.create(**payload)
            offering.allowed_customers.add(tenant.service_project_link.project.customer)


def archive_offering(sender, instance, **kwargs):
    service_settings = instance

    if service_settings.type == openstack_tenant_apps.OpenStackTenantConfig.service_name and \
            service_settings.content_type == ContentType.objects.get_for_model(openstack_models.Tenant):
        marketplace_models.Offering.objects.filter(scope=service_settings).update(
            state=marketplace_models.Offering.States.ARCHIVED
        )


def synchronize_volume_metadata(sender, instance, created=False, **kwargs):
    volume = instance
    if not created and not set(volume.tracker.changed()) & {'size', 'instance_id'}:
        return

    try:
        resource = marketplace_models.Resource.objects.get(scope=volume)
    except ObjectDoesNotExist:
        logger.debug('Skipping resource synchronization for OpenStack volume '
                     'because marketplace resource does not exist. '
                     'Resource ID: %s', instance.id)
        return

    utils.import_volume_metadata(resource)


def synchronize_instance_metadata(sender, instance, created=False, **kwargs):
    if not created and not set(instance.tracker.changed()) & {'name'}:
        return

    for volume in instance.volumes.all():
        try:
            resource = marketplace_models.Resource.objects.get(scope=volume)
        except ObjectDoesNotExist:
            logger.debug('Skipping resource synchronization for OpenStack volume '
                         'because marketplace resource does not exist. '
                         'Resource ID: %s', instance.id)
            continue

        resource.backend_metadata['instance_name'] = volume.instance.name
        resource.save(update_fields=['backend_metadata'])


def synchronize_internal_ips(sender, instance, created=False, **kwargs):
    internal_ip = instance
    if not created and not set(internal_ip.tracker.changed()) & {'ip4_address', 'instance_id'}:
        return

    vms = {vm for vm in (internal_ip.instance_id, internal_ip.tracker.previous('instance_id')) if vm}

    for vm in vms:
        try:
            scope = openstack_tenant_models.Instance.objects.get(id=vm)
            resource = marketplace_models.Resource.objects.get(scope=scope)
        except ObjectDoesNotExist:
            logger.debug('Skipping resource synchronization for OpenStack instance '
                         'because marketplace resource does not exist. '
                         'Resource ID: %s', vm)
            continue

        utils.import_instance_metadata(resource)


def synchronize_floating_ips(sender, instance, created=False, **kwargs):
    floating_ip = instance
    if not created and not set(instance.tracker.changed()) & {'address', 'internal_ip_id'}:
        return

    internal_ips = {ip for ip in (floating_ip.internal_ip_id, floating_ip.tracker.previous('internal_ip_id')) if ip}
    for ip_id in internal_ips:
        try:
            scope = openstack_tenant_models.Instance.objects.get(internal_ips_set__id=ip_id)
            resource = marketplace_models.Resource.objects.get(scope=scope)
        except ObjectDoesNotExist:
            logger.debug('Skipping resource synchronization for OpenStack instance '
                         'because marketplace resource does not exist. '
                         'Resource ID: %s', ip_id)
            continue

        utils.import_instance_metadata(resource)
