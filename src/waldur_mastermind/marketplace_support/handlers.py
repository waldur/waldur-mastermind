import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support import models as support_models

logger = logging.getLogger(__name__)


def create_support_template(sender, instance, created=False, **kwargs):
    if instance.type != PLUGIN_NAME or not created:
        return

    if not instance.scope:
        template = support_models.OfferingTemplate.objects.create(
            name=instance.name,
            config=instance.options
        )
        instance.scope = template
        instance.save()


def change_order_item_state(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.tracker.has_changed('state'):
        return

    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.warning('Skipping support offering state synchronization '
                       'because related order item is not found. Offering ID: %s', instance.id)
        return

    if instance.state == support_models.Offering.States.OK:
        callbacks.resource_creation_succeeded(resource)
    elif instance.state == support_models.Offering.States.TERMINATED:
        if instance.tracker.previous('state') == support_models.Offering.States.REQUESTED:
            callbacks.resource_creation_failed(resource)
        if instance.tracker.previous('state') == support_models.Offering.States.OK:
            callbacks.resource_deletion_succeeded(resource)


def terminate_resource(sender, instance, **kwargs):
    try:
        resource = marketplace_models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.debug('Skipping resource terminate for support request '
                     'because related resource does not exist. '
                     'Request ID: %s', instance.id)
    else:
        callbacks.resource_deletion_succeeded(resource)


def create_support_plan(sender, instance, created=False, **kwargs):
    plan = instance
    if plan.offering.type != PLUGIN_NAME or not created:
        return

    if not isinstance(plan.offering.scope, support_models.OfferingTemplate):
        return

    with transaction.atomic():
        if not plan.scope:
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


def change_offering_state(sender, instance, created=False, **kwargs):
    """ Processing of creating support offering issue."""
    if created:
        return

    issue = instance

    if instance.tracker.has_changed('status') and issue.resolved is not None:
        try:
            offering = support_models.Offering.objects.get(issue=issue)
        except support_models.Offering.DoesNotExist:
            logger.warning('Skipping issue state synchronization '
                           'because related support offering is not found. Issue ID: %s', issue.id)
            return

        if issue.resolved:
            offering.state = support_models.Offering.States.OK
        else:
            offering.state = support_models.Offering.States.TERMINATED

        offering.save()


def update_order_item_if_issue_was_complete(sender, instance, created=False, **kwargs):
    """ Processing of terminating or updating a resource."""
    if created:
        return

    issue = instance

    if not issue.tracker.has_changed('status'):
        return

    if issue.resource \
            and isinstance(issue.resource, marketplace_models.OrderItem) \
            and issue.resource.offering.type == PLUGIN_NAME and issue.resolved is not None:
        order_item = issue.resource

        if issue.resolved:
            request = order_item.resource.scope
            # A request is object of support.Offering.
            # Support.Offering object created from a marketplace is called 'request' in a frontend

            if not request:
                logger.warning('Skipping resource termination '
                               'because request is not found. Order item ID: %s', order_item.id)
                return

            with transaction.atomic():
                if order_item.type == marketplace_models.OrderItem.Types.TERMINATE:
                    request.delete()
                    # callbacks.resource_deletion_succeeded will called in terminate_resource handler
                elif order_item.type == marketplace_models.OrderItem.Types.UPDATE:
                    if request.issue != issue:
                        request.issue = issue
                        request.save(update_fields=['issue'])
                    callbacks.resource_update_succeeded(order_item.resource)
        else:
            with transaction.atomic():
                if order_item.type == marketplace_models.OrderItem.Types.TERMINATE:
                    callbacks.resource_deletion_failed(order_item.resource)
                elif order_item.type == marketplace_models.OrderItem.Types.UPDATE:
                    callbacks.resource_update_failed(order_item.resource)
