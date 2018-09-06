from __future__ import unicode_literals

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


def order_set_state_done(sender, instance, created=False, **kwargs):
    if created:
        return

    if instance.tracker.has_changed('state') and instance.state in models.OrderItem.States.TERMINAL_STATES:
        order = instance.order

        # check if there are any non-finished OrderItems left and finish order if none is found
        if not models.OrderItem.objects.filter(order=order).\
                exclude(state__in=models.OrderItem.States.TERMINAL_STATES).exists():
            order.set_state_done()
            order.save(update_fields=['state'])
