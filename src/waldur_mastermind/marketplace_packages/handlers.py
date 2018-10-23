import logging

from django.db import transaction

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_packages import PLUGIN_NAME
from waldur_mastermind.packages import models as package_models
from waldur_openstack.openstack.apps import OpenStackConfig

logger = logging.getLogger(__name__)


def create_template_for_plan(sender, instance, created=False, **kwargs):
    plan = instance

    if not created:
        return

    if plan.offering.type != PLUGIN_NAME:
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

    if component.plan.offering.type != PLUGIN_NAME:
        return

    template = component.plan.scope
    if not template:
        logger.warning('Skipping plan component synchronization because offering does not have scope. '
                       'Offering ID: %s', component.plan.offering.id)
        return

    package_models.PackageComponent.objects.create(
        template=template,
        type=component.component.type,
        amount=component.amount,
        price=component.price,
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
            openstack_package = package_models.OpenStackPackage.objects.get(tenant=instance)
            order_item = marketplace_models.OrderItem.objects.get(scope=openstack_package)
        except package_models.OpenStackPackage.DoesNotExist:
            return
        except marketplace_models.OrderItem.DoesNotExist:
            return

        if instance.state == instance.States.OK:
            order_item.set_state_done()
            order_item.save(update_fields=['state'])

        if instance.state == instance.States.ERRED:
            order_item.set_state_erred()
            order_item.save(update_fields=['state'])
