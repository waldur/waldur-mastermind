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


def offering_set_state_ok(sender, instance, created=False, **kwargs):
    if created:
        return

    if instance.tracker.has_changed('resolution'):
        issue = instance
        try:
            offering = support_models.Offering.objects.get(issue=issue)
        except support_models.Offering.DoesNotExist:
            logger.warning('Skipping issue state synchronization '
                           'because related support offering is not found. Issue ID: %s', issue.id)
            return

        if issue.resolution:
            offering.state = support_models.Offering.States.OK
            offering.save()


def update_order_item_if_issue_was_complete(sender, instance, created=False, **kwargs):
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
                order_item.resource.set_state_terminated()
                order_item.resource.save(update_fields=['state'])
                request.delete()
        else:
            order_item.resource.set_state_erred()
            order_item.resource.save(update_fields=['state'])

            order_item.set_state_erred()
            order_item.save(update_fields=['state'])


def synchronize_terminated_status_for_resource_and_scope(sender, instance, created=False, **kwargs):
    if created:
        return

    resource = instance

    if not resource.tracker.has_changed('state'):
        return

    if resource.state != marketplace_models.Resource.States.TERMINATED:
        return

    if resource.offering.type != 'Support.OfferingTemplate':
        return

    resource.scope.state = support_models.Offering.States.TERMINATED
    resource.scope.save(update_fields=['state'])
