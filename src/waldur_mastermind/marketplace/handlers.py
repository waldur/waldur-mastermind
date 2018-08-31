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

    if instance.tracker.has_changed('state') and instance.state == models.OrderItem.States.DONE:
        order = instance.order

        for item in order.items.all():
            if item.state != models.OrderItem.States.DONE:
                return

        order.set_state_done()
        order.save(update_fields=['state'])
