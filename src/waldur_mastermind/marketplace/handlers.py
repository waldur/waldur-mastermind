from __future__ import unicode_literals

import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, signals
from django.db import transaction
from django.utils.timezone import now

from waldur_core.core import utils as core_utils
from waldur_core.structure.models import Project, Customer

from . import callbacks, tasks, log, models, utils


logger = logging.getLogger(__name__)


def create_screenshot_thumbnail(sender, instance, created=False, **kwargs):
    if not created:
        return

    transaction.on_commit(lambda: tasks.create_screenshot_thumbnail.delay(instance.uuid))


def log_order_events(sender, instance, created=False, **kwargs):
    order = instance
    if created:
        # Skip logging for imported orders
        if order.state != models.Order.States.DONE:
            log.log_order_created(order)
    elif not order.tracker.has_changed('state'):
        return
    elif order.state == models.Order.States.EXECUTING:
        log.log_order_approved(order)
    elif order.state == models.Order.States.REJECTED:
        log.log_order_rejected(order)
    elif order.state == models.Order.States.DONE:
        log.log_order_completed(order)
    elif order.state == models.Order.States.TERMINATED:
        log.log_order_terminated(order)
    elif order.state == models.Order.States.ERRED:
        log.log_order_failed(order)


def log_order_item_events(sender, instance, created=False, **kwargs):
    order_item = instance
    if not created:
        return
    if order_item.state != models.OrderItem.States.PENDING:
        # Skip logging for imported order item
        return
    elif not order_item.resource:
        return
    elif order_item.type == models.OrderItem.Types.TERMINATE:
        log.log_resource_terminate_requested(order_item.resource)
    elif order_item.type == models.OrderItem.Types.UPDATE:
        log.log_resource_update_requested(order_item.resource)


def log_resource_events(sender, instance, created=False, **kwargs):
    resource = instance
    # Skip logging for imported resource
    if created and instance.state == models.Resource.States.CREATING:
        log.log_resource_creation_requested(resource)


def complete_order_when_all_items_are_done(sender, instance, created=False, **kwargs):
    if created:
        return

    if not instance.tracker.has_changed('state'):
        return

    if instance.state not in models.OrderItem.States.TERMINAL_STATES:
        return

    if instance.order.state != models.Order.States.EXECUTING:
        return

    order = instance.order
    # check if there are any non-finished OrderItems left and finish order if none is found
    if models.OrderItem.objects.filter(order=order).\
            exclude(state__in=models.OrderItem.States.TERMINAL_STATES).exists():
        return

    order.complete()
    order.save(update_fields=['state'])


def update_category_quota_when_offering_is_created(sender, instance, created=False, **kwargs):
    def get_delta():
        if created:
            if instance.state == models.Offering.States.ACTIVE:
                return 1
        else:
            if instance.tracker.has_changed('state'):
                if instance.state == models.Offering.States.ACTIVE:
                    return 1
                elif instance.tracker.previous('state') == models.Offering.States.ACTIVE:
                    return -1

    delta = get_delta()
    if delta:
        instance.category.add_quota_usage(models.Category.Quotas.offering_count, delta)


def update_category_quota_when_offering_is_deleted(sender, instance, **kwargs):
    if instance.state == models.Offering.States.ACTIVE:
        instance.category.add_quota_usage(models.Category.Quotas.offering_count, -1)


def update_category_offerings_count(sender, **kwargs):
    for category in models.Category.objects.all():
        value = models.Offering.objects.filter(category=category,
                                               state=models.Offering.States.ACTIVE).count()
        category.set_quota_usage(models.Category.Quotas.offering_count, value)


def update_aggregate_resources_count_when_resource_is_updated(sender, instance, created=False, **kwargs):
    def apply_change(delta):
        for field in ('project', 'customer'):
            counter, _ = models.AggregateResourceCount.objects.get_or_create(
                scope=getattr(instance, field),
                category=instance.offering.category,
            )
            if delta == 1:
                counter.count += 1
            elif delta == -1:
                counter.count = max(0, counter.count - 1)

            counter.save(update_fields=['count'])

    if instance.scope and (created or not instance.tracker.previous('object_id')):
        apply_change(1)
    elif not instance.scope and instance.tracker.previous('object_id'):
        apply_change(-1)
    elif instance.tracker.has_changed('state') and instance.state == models.Resource.States.TERMINATED:
        apply_change(-1)


def update_aggregate_resources_count(sender, **kwargs):
    for category in models.Category.objects.all():
        for field, content_type in (
            ('project_id', ContentType.objects.get_for_model(Project)),
            ('project__customer_id', ContentType.objects.get_for_model(Customer)),
        ):
            rows = models.Resource.objects\
                .filter(offering__category=category)\
                .exclude(state=models.Resource.States.TERMINATED)\
                .values(field, 'offering__category')\
                .annotate(count=Count('*'))
            for row in rows:
                models.AggregateResourceCount.objects.update_or_create(
                    content_type=content_type,
                    object_id=row[field],
                    category=category,
                    defaults={'count': row['count']},
                )


def close_resource_plan_period_when_resource_is_terminated(sender, instance, created=False, **kwargs):
    """
    Handle case when resource has been terminated by service provider.
    """

    if created:
        return

    if not instance.tracker.has_changed('state'):
        return

    if instance.state != models.Resource.States.TERMINATED:
        return

    if instance.tracker.previous('state') == models.Resource.States.TERMINATING:
        # It is expected that this case is handled using callbacks
        return

    if not instance.plan:
        return

    models.ResourcePlanPeriod.objects.filter(
        resource=instance,
        plan=instance.plan,
        end=None
    ).update(end=now())


def reject_order(sender, instance, created=False, **kwargs):
    if created:
        return

    order = instance

    if not order.tracker.has_changed('state'):
        return

    if instance.tracker.previous('state') == models.Order.States.REQUESTED_FOR_APPROVAL and \
            order.state == models.Order.States.REJECTED:
        for item in order.items.all():
            item.set_state_terminated()
            item.save(update_fields=['state'])


def change_order_item_state(sender, instance, created=False, **kwargs):
    if created or not instance.tracker.has_changed('state'):
        return

    try:
        resource = models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.warning('Skipping resource state synchronization '
                       'because marketplace resource is not found. '
                       'Resource ID: %s', core_utils.serialize_instance(instance))
    else:
        callbacks.sync_resource_state(instance, resource)


def terminate_resource(sender, instance, **kwargs):
    try:
        resource = models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.debug('Skipping terminate for resource '
                     'because marketplace resource does not exist. '
                     'Resource ID: %s', core_utils.serialize_instance(instance))
    else:
        callbacks.resource_deletion_succeeded(resource)


def connect_resource_handlers(*resources):
    for index, model in enumerate(resources):
        suffix = '%s_%s' % (index, model.__class__)

        signals.post_save.connect(
            change_order_item_state,
            sender=model,
            dispatch_uid='waldur_mastermind.marketpace.change_order_item_state_%s' % suffix,
        )

        signals.pre_delete.connect(
            terminate_resource,
            sender=model,
            dispatch_uid='waldur_mastermind.marketpace.terminate_resource_%s' % suffix,
        )


def synchronize_resource_metadata(sender, instance, created=False, **kwargs):
    fields = {'action', 'action_details', 'state', 'runtime_state', 'name'}
    if not created and not set(instance.tracker.changed()) & fields:
        return

    try:
        resource = models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.debug('Skipping resource synchronization for OpenStack resource '
                     'because marketplace resource does not exist. '
                     'Resource ID: %s', instance.id)
        return

    utils.import_resource_metadata(resource)


def connect_resource_metadata_handlers(*resources):
    for index, model in enumerate(resources):
        signals.post_save.connect(
            synchronize_resource_metadata,
            sender=model,
            dispatch_uid='waldur_mastermind.marketpace.'
                         'synchronize_resource_metadata_%s_%s' % (index, model.__class__),
        )
