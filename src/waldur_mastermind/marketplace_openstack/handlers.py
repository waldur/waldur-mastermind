import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.packages import models as package_models
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.apps import OpenStackConfig
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps

from . import INSTANCE_TYPE, PACKAGE_TYPE, VOLUME_TYPE, utils

logger = logging.getLogger(__name__)


def create_template_for_plan(sender, instance, created=False, **kwargs):
    plan = instance

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
        package_models.PackageComponent.objects.create(
            template=template,
            type=component.component.type,
            amount=component.amount,
            price=component.price,
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


def change_order_item_state(sender, instance, created=False, **kwargs):
    if created or not instance.tracker.has_changed('state'):
        return

    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.warning('Skipping OpenStack resource state synchronization '
                       'because marketplace resource is not found. '
                       'Resource ID: %s', instance.id)
    else:
        callbacks.sync_resource_state(instance, resource)


def terminate_resource(sender, instance, **kwargs):
    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.debug('Skipping resource terminate for OpenStack resource'
                     'because marketplace resource does not exist. '
                     'Resource ID: %s', instance.id)
    else:
        callbacks.resource_deletion_succeeded(resource)
