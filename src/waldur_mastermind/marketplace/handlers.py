from __future__ import unicode_literals

from django.db.models import Count
from django.db import transaction

from . import tasks, models


def create_screenshot_thumbnail(sender, instance, created=False, **kwargs):
    if not created:
        return

    transaction.on_commit(lambda: tasks.create_screenshot_thumbnail.delay(instance.uuid))


def notifications_order_approval(sender, instance, created=False, **kwargs):
    if not created:
        return

    transaction.on_commit(lambda: tasks.notify_order_approvers.delay(instance.uuid))


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
        instance.category.add_quota_usage(models.Category.Quotas.offering_count, -1, fail_silently=True)


def update_category_offerings_count(sender, **kwargs):
    for category in models.Category.objects.all():
        value = models.Offering.objects.filter(category=category,
                                               state=models.Offering.States.ACTIVE).count()
        category.set_quota_usage(models.Category.Quotas.offering_count, value)


def update_project_resources_count_when_resource_is_updated(sender, instance, created=False, **kwargs):
    def apply_change(delta):
        counter, _ = models.ProjectResourceCount.objects.get_or_create(
            project=instance.project,
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


def update_project_resources_count(sender, **kwargs):
    rows = models.Resource.objects\
        .exclude(state=models.Resource.States.TERMINATED)\
        .values('project', 'offering__category')\
        .annotate(count=Count('*'))
    for row in rows:
        models.ProjectResourceCount.objects.update_or_create(
            project_id=row['project'],
            category_id=row['offering__category'],
            defaults={'count': row['count']},
        )
