import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps
from waldur_openstack.openstack import models as openstack_models

from . import PLUGIN_NAME, utils

logger = logging.getLogger(__name__)


def create_offering_from_tenant(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.tracker.has_changed('state'):
        return

    if instance.tracker.previous('state') != instance.States.CREATING:
        return

    if instance.state != instance.States.OK:
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
    payload = dict(
        type=PLUGIN_NAME,
        name=utils.get_offering_name_for_tenant(parent_offering),
        scope=service_settings,
        shared=False,
    )

    fields = (
        'state',
        'customer',
        'category',
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


def change_order_item_state(sender, instance, created=False, **kwargs):
    if created or not instance.tracker.has_changed('state'):
        return

    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.warning('Skipping OpenStack volume state synchronization '
                       'because related resource is not found. Volume ID: %s', instance.id)
    else:
        callbacks.sync_resource_state(instance, resource)


def terminate_resource(sender, instance, **kwargs):
    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.debug('Skipping resource terminate for OpenStack volume'
                     'because related resource does not exist. '
                     'Volume ID: %s', instance.id)
    else:
        callbacks.resource_deletion_succeeded(resource)
