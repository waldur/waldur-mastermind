import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps

from . import PLUGIN_NAME

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
        order_item = marketplace_models.OrderItem.objects.get(scope=tenant)
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

    parent_offering = order_item.offering
    payload = dict(
        type=PLUGIN_NAME,
        name='Virtual machine in %s' % parent_offering.name,
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
    tenant = instance

    try:
        service_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name,
        )
    except ObjectDoesNotExist:
        logger.debug('Skipping offering creation for tenant because service settings '
                     'object does not exist. OpenStack tenant ID: %s', tenant.id)
        return

    marketplace_models.Offering.objects.filter(scope=service_settings).update(
        state=marketplace_models.Offering.States.ARCHIVED
    )


def change_order_item_state(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.tracker.has_changed('state'):
        return

    if instance.tracker.previous('state') != instance.States.CREATING:
        return

    if instance.state in [instance.States.OK, instance.States.ERRED]:
        try:
            order_item = marketplace_models.OrderItem.objects.get(scope=instance)
        except ObjectDoesNotExist:
            logger.debug('Skipping OpenStack instance state synchronization with marketplace '
                         'because order item does not exist. OpenStack instance ID: %s', instance.id)
            return

        if instance.state == instance.States.OK:
            order_item.set_state_done()
            order_item.save(update_fields=['state'])

        if instance.state == instance.States.ERRED:
            order_item.set_state_erred()
            order_item.save(update_fields=['state'])
