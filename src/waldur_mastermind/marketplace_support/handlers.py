import logging

from django.db import transaction

from waldur_mastermind.support import models as support_models
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.marketplace.models import OrderItem


logger = logging.getLogger(__name__)


def create_support_template(sender, instance, created=False, **kwargs):
    if instance.type != PLUGIN_NAME or not created:
        return

    template = support_models.OfferingTemplate.objects.create(
        name=instance.name,
        config=instance.options
    )
    instance.scope = template
    instance.save()


def change_order_item_state(sender, instance, created=False, **kwargs):
    if created:
        return

    if instance.tracker.has_changed('state'):
        try:
            order_item = OrderItem.objects.get(scope=instance)
        except OrderItem.DoesNotExist:
            logger.warning('Skipping support offering state synchronization '
                           'because related order item is not found. Offering ID: %s', instance.id)
            return

        if instance.state == support_models.Offering.States.OK:
            order_item.set_state_done()
            order_item.save(update_fields=['state'])

        if instance.state == support_models.Offering.States.TERMINATED:
            order_item.set_state_terminated()
            order_item.save(update_fields=['state'])


def create_support_plan(sender, instance, created=False, **kwargs):
    plan = instance
    if plan.offering.type != PLUGIN_NAME or not created:
        return

    with transaction.atomic():
        offering_plan = support_models.OfferingPlan.objects.create(
            template=plan.offering.scope,
            name=plan.name,
            description=plan.description,
            product_code=plan.product_code,
            article_code=plan.article_code,
            unit=plan.unit,
            unit_price=plan.unit_price,
        )
        plan.scope = offering_plan
        plan.save()
